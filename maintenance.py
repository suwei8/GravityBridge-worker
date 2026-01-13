import os
import json
import requests
import subprocess
import argparse
import time
import shutil

# Secrets from Environment
AGENTS_JSON_URL = os.getenv("AGENTS_JSON_URL")
SSH_USER = os.getenv("SSH_USERNAME", "sw")
SSH_PASS = os.getenv("SSH_PASSWORD")
GH_TOKEN = os.getenv("GITHUB_TOKEN")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Cloudflare Secrets (For Deployment)
CF_API_EMAIL = os.getenv("CF_API_EMAIL")
CF_API_KEY = os.getenv("CF_API_KEY")
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_ZONE_ID = os.getenv("CF_ZONE_ID")

def redact_secrets(text):
    if not text: return text
    # Redact Token in URL
    import re
    text = re.sub(r'token=[^&\s]+', 'token=***', str(text))
    # Redact SSH Pass if somehow present
    if SSH_PASS:
        text = text.replace(SSH_PASS, '***')
    return text

def send_telegram(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        # Don't print the token even in debug
        print(f"⚠️ Telegram config missing, skipping msg: {redact_secrets(message)}")
        return
    
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=5)
    except Exception as e:
        print(f"❌ Failed to send Telegram: {redact_secrets(e)}")

def get_agents():
    # Safe log
    safe_url = redact_secrets(AGENTS_JSON_URL)
    print(f"Fetching agents from {safe_url}...")
    try:
        resp = requests.get(AGENTS_JSON_URL)
        resp.raise_for_status()
        data = resp.json()
        return data.get("agents", {})
    except Exception as e:
        msg = f"❌ **GravityBridge Alert**\nFailed to fetch `agents.json`: {redact_secrets(e)}"
        print(msg)
        send_telegram(msg)
        return {}

def get_latest_version():
    url = "https://api.github.com/repos/suwei8/GravityBridge-Go/releases/latest"
    headers = {"Authorization": f"token {GH_TOKEN}"} if GH_TOKEN else {}
    try:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["tag_name"]
    except Exception as e:
        print(f"⚠️ Failed to fetch latest version: {e}")
        return None

def run_ssh(host, cmd):
    # Assumes cloudflared is installed and configured in ~/.ssh/config or via ProxyCommand
    ssh_cmd = [
        "sshpass", "-p", SSH_PASS,
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        f"{SSH_USER}@{host}",
        cmd
    ]
    return subprocess.run(ssh_cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

def resolve_tunnel_id(hostname):
    """Resolve Cloudflare Tunnel ID for a given hostname CNAME."""
    if not CF_API_EMAIL or not CF_API_KEY or not CF_ZONE_ID:
        print("⚠️ Missing Cloudflare Credentials, cannot resolve Tunnel ID automatically.")
        return None

    url = f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records?name={hostname}&type=CNAME"
    headers = {
        "X-Auth-Email": CF_API_EMAIL,
        "X-Auth-Key": CF_API_KEY,
        "Content-Type": "application/json"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data["success"] and data["result"]:
            content = data["result"][0]["content"]
            # content is like <uuid>.cfargotunnel.com
            import re
            match = re.search(r"([a-f0-9-]+)\.cfargotunnel\.com", content)
            if match:
                return match.group(1)
    except Exception as e:
        print(f"❌ Failed to resolve Tunnel ID for {hostname}: {e}")
    return None

def restart_services(agents):
    for name, info in agents.items():
        if isinstance(info, str): continue
        ssh_host = info.get("ssh_host")
        if not ssh_host: continue

        print(f"🔄 Restarting {name}...")
        cmd = "pkill -9 -f gravity-agent; nohup ~/gravity-agent/gravity-agent > ~/gravity-agent/agent.log 2>&1 &"
        ret = run_ssh(ssh_host, cmd)
        
        if ret.returncode == 0:
            print(f"✅ {name}: Restart Triggered")
        else:
            print(f"❌ {name}: Restart Failed: {ret.stderr}")

def deploy_agent(name, agents, args):
    info = agents.get(name)
    if not info:
        print(f"❌ Agent {name} not found in agents.json")
        return

    # Handle object structure
    if isinstance(info, str):
        print(f"❌ Legacy agent format for {name} (no ssh_host). Cannot deploy.")
        return
        
    ssh_host = info.get("ssh_host")
    public_url = info.get("url")
    
    if not ssh_host:
        print(f"❌ Missing ssh_host for {name}")
        return

    print(f"🚀 Deploying {name} to {ssh_host}...")

    # 1. Resolve Data
    vpc_host = ssh_host # Source of Truth for Tunnel ID
    vless_host = public_url.replace("https://", "").replace("http://", "")
    
    # Strict Automation: Resolve or Fail
    print(f"🔍 Resolving Tunnel ID via DNS for SSH Host: {vpc_host}...")
    tunnel_id = resolve_tunnel_id(vpc_host)
    
    if not tunnel_id:
        # Check if user provided an override (still useful for debugging but not relied upon)
        if args.tunnel_id:
            print(f"⚠️ DNS Resolution failed, but using manual override: {args.tunnel_id}")
            tunnel_id = args.tunnel_id
        else:
            msg = f"❌ **Deployment Failed**: Could not resolve Tunnel ID for `{vpc_host}`.\nEnsure the server has a Cloudflare Tunnel running and the DNS record exists."
            print(msg)
            send_telegram(msg)
            return

    print(f"✅ Resolved Tunnel ID: {tunnel_id}")
    
    # 2. Prepare Environment File
    env_content = f"""AGENT_NAME={name}
TUNNEL_ID={tunnel_id}
PUBLIC_URL={public_url}
WORKER_URL=https://gravity-bridge-worker.58.workers.dev
GITHUB_TOKEN={GH_TOKEN}
HEADLESS=true
"""
    # Write temp .env
    with open(".env.tmp", "w") as f:
        f.write(env_content)
    
    # 3. Download Latest Binary
    if not os.path.exists("gravity-agent"):
        print("⬇️ Downloading latest binary...")
        latest_ver = get_latest_version()
        if not latest_ver:
            print("❌ Start failed: Cannot determine version")
            return
            
        down_url = f"https://github.com/suwei8/GravityBridge-Go/releases/download/{latest_ver}/gravity-agent-linux-arm64"
        print(f"Fetching {down_url}...")
        resp = requests.get(down_url, stream=True)
        if resp.status_code == 200:
            with open("gravity-agent", "wb") as f:
                shutil.copyfileobj(resp.raw, f)
            os.chmod("gravity-agent", 0o755)
        else:
            print(f"❌ Failed to download binary: {resp.status_code}")
            return

    # 4. Transfer Files
    run_ssh(ssh_host, "mkdir -p ~/gravity-agent")
    
    # Transfer Binary
    print("📤 Transferring binary...")
    subprocess.run(["sshpass", "-p", SSH_PASS, "scp", "-o", "StrictHostKeyChecking=no", "gravity-agent", f"{SSH_USER}@{ssh_host}:~/gravity-agent/"])
    
    # Transfer Templates (Recursive)
    if os.path.exists("templates"):
        print("📤 Transferring templates...")
        subprocess.run(["sshpass", "-p", SSH_PASS, "scp", "-r", "-o", "StrictHostKeyChecking=no", "templates", f"{SSH_USER}@{ssh_host}:~/gravity-agent/"])
    else:
        print("⚠️ Warning: No 'templates' directory found in workspace. UI automation will fail.")

    # Transfer .env
    print("📤 Transferring config...")
    subprocess.run(["sshpass", "-p", SSH_PASS, "scp", "-o", "StrictHostKeyChecking=no", ".env.tmp", f"{SSH_USER}@{ssh_host}:~/gravity-agent/.env"])
    
    # 5. Restart
    restart_services({name: info})
    
    print(f"✅ Deployment of {name} Complete.")

def check_deploy(agents):
    latest_ver = get_latest_version()
    print(f"Latest Version: {latest_ver}")
    
    missing_config = []
    
    for name, info in agents.items():
        if isinstance(info, str):
            print(f"⚠️ Skipping {name}: Missing ssh_host (Legacy Format)")
            missing_config.append(name)
            continue
        
        ssh_host = info.get("ssh_host")
        if not ssh_host:
            print(f"⚠️ Skipping {name}: Missing ssh_host field")
            missing_config.append(name)
            continue
            
        print(f"checking {name} ({ssh_host})...")
        ret = run_ssh(ssh_host, "pgrep -f gravity-agent")
        
        if ret.returncode == 0:
            print(f"✅ {name}: Service Running")
        else:
            print(f"❌ {name}: Service NOT Running")

    if missing_config:
        msg = f"⚠️ **Configuration Missing**\nThe following agents lack `ssh_host` config:\n`{', '.join(missing_config)}`\nPlease update `agents.json`."
        send_telegram(msg)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=["check", "restart", "deploy"], required=True)
    parser.add_argument("--target", help="Specific agent name to target (required for deploy)")
    parser.add_argument("--tunnel-id", help="Manually specify Tunnel ID for new deployments")
    args = parser.parse_args()
    
    agents = get_agents()
    
    if args.action == "deploy":
        if not args.target:
            print("❌ --target is required for deploy action")
            return
        deploy_agent(args.target, agents, args)
    elif args.action == "check":
        check_deploy(agents)
    elif args.action == "restart":
        if args.target:
            agents = {k:v for k,v in agents.items() if k == args.target}
        restart_services(agents)

if __name__ == "__main__":
    main()
