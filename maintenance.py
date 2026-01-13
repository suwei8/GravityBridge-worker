import os
import json
import requests
import subprocess
import argparse
import time
import shutil

# Secrets from Environment
# Secrets from Environment
AGENTS_JSON_URL = os.getenv("AGENTS_JSON_URL", "").strip()
SSH_USER = os.getenv("SSH_USERNAME", "sw").strip()
SSH_PASS = os.getenv("SSH_PASSWORD", "").strip()
GH_TOKEN = os.getenv("GH_TOKEN", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Cloudflare Secrets (Account A: hhwpxh.com)
CF_API_EMAIL = os.getenv("CF_API_EMAIL", "").strip()
CF_API_KEY = os.getenv("CF_API_KEY", "").strip()
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID", "").strip()
CF_ZONE_ID = os.getenv("CF_ZONE_ID", "").strip()

# Cloudflare Secrets (Account B: 555606.xyz)
CF_API_EMAIL_B = os.getenv("CF_API_EMAIL_555606", "").strip()
CF_API_KEY_B = os.getenv("CF_API_KEY_555606", "").strip()
CF_ACCOUNT_ID_B = os.getenv("CF_ACCOUNT_ID_555606", "").strip()
CF_ZONE_ID_B = os.getenv("CF_ZONE_ID_555606", "").strip()

def redact_secrets(text):
    # ... (existing redaction code) ...
    if not text: return text
    import re
    text = re.sub(r'token=[^&\s]+', 'token=***', str(text))
    if SSH_PASS: text = text.replace(SSH_PASS, '***')
    return text

# ... (existing send_telegram) ...

def get_agents():
    # Attempt 1: Fetch via GitHub API (Preferred if GH_TOKEN is valid for cross-repo)
    if GH_TOKEN:
        print("Fetching agents via GitHub API...")
        api_url = "https://api.github.com/repos/suwei8/GravityBridge-Go/contents/.agent/data/agents.json"
        headers = {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        try:
            resp = requests.get(api_url, headers=headers)
            if resp.status_code == 200:
                import base64
                content = base64.b64decode(resp.json()["content"]).decode("utf-8")
                data = json.loads(content)
                return data.get("agents", {})
            elif resp.status_code == 404:
                print("⚠️ API returned 404. Check Repo/Path permissions.")
            else:
                 print(f"⚠️ API Fetch failed: {resp.status_code} {resp.text}")
        except Exception as e:
            print(f"⚠️ API Fetch Exception: {redact_secrets(e)}")

    # Attempt 2: Fallback to Raw URL (if provided)
    if AGENTS_JSON_URL:
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
            
    print("❌ No valid method to fetch agents.json")
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

def get_cloudflare_ctx(hostname):
    """Select the correct Cloudflare credentials based on domain."""
    if hostname.endswith("555606.xyz"):
        return {
            "email": CF_API_EMAIL_B,
            "key": CF_API_KEY_B,
            "zone_id": CF_ZONE_ID_B
        }
    else:
        # Default to Account A (hhwpxh.com) or fallback
        return {
            "email": CF_API_EMAIL,
            "key": CF_API_KEY,
            "zone_id": CF_ZONE_ID
        }

def resolve_tunnel_id(hostname):
    """Resolve Cloudflare Tunnel ID for a given hostname CNAME."""
    ctx = get_cloudflare_ctx(hostname)
    
    if not ctx["email"] or not ctx["key"] or not ctx["zone_id"]:
        print(f"⚠️ Missing Cloudflare Credentials for {hostname}, cannot resolve Tunnel ID automatically.")
        return None

    url = f"https://api.cloudflare.com/client/v4/zones/{ctx['zone_id']}/dns_records?name={hostname}&type=CNAME"
    headers = {
        "X-Auth-Email": ctx["email"],
        "X-Auth-Key": ctx["key"],
        "Content-Type": "application/json"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data["success"] and data["result"]:
            content = data["result"][0]["content"]
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
        
        # 1. Ensure binary is executable (SCP might lose permissions)
        run_ssh(ssh_host, "chmod +x ~/gravity-agent/gravity-agent")

        # 2. Restart (Ignore pkill failure if process doesn't exist)
        # Use -x (exact match) to avoid killing the SSH command itself which contains "gravity-agent"
        # Target REAL Desktop (User confirmed desktop environment exists). Usually :0 or :1.
        # We try :10.0 (SSH X11) or :0 (Local). Since it's a desktop server, likely :0.
        # We also add xhost + just in case permissions are localized
        cmd = "(pkill -9 -x gravity-agent || true); export DISPLAY=:0; xhost +local: >/dev/null 2>&1 || true; nohup ~/gravity-agent/gravity-agent > ~/gravity-agent/agent.log 2>&1 &"
        ret = run_ssh(ssh_host, cmd)
        
        if ret.returncode == 0:
            print(f"✅ {name}: Restart Triggered")
        else:
            print(f"❌ {name}: Restart Failed. Exit Code: {ret.returncode}")
            print(f"   Stdout: {ret.stdout}")
            print(f"   Stderr: {ret.stderr}")

def debug_agent(name, agents):
    info = agents.get(name)
    if not info:
        print(f"❌ Agent {name} not found")
        return
        
    ssh_host = info.get("ssh_host")
    if not ssh_host:
        print(f"❌ No ssh_host for {name}")
        return

    print(f"🔍 Debugging {name} ({ssh_host})...")
    
    commands = [
        ("Process Status", "pgrep -a gravity-agent || echo 'Not Running'"),
        ("File Permissions", "ls -la ~/gravity-agent/"),
        ("Agent Log (Last 200 lines)", "tail -n 200 ~/gravity-agent/agent.log || echo 'No Log'"),
        ("Env File Check", "cat ~/gravity-agent/.env || echo 'No Env'"),
        ("Env File Check", "cat ~/gravity-agent/.env || echo 'No Env'"),
        ("Binary Test (Version/Help)", "~/gravity-agent/gravity-agent --help || echo 'Binary Exec Failed'"),
        ("Architecture Check", "uname -a"),
        ("Display Check", "ls -la /tmp/.X11-unix/ || echo 'No X11 Sockets'"),
        ("Active Users", "w || echo 'w failed'")
    ]

    for title, cmd in commands:
        print(f"\n--- {title} ---")
        ret = run_ssh(ssh_host, cmd)
        print(f"Exit: {ret.returncode}")
        print(ret.stdout)
        if ret.stderr:
            print(f"Stderr: {ret.stderr}")

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
        print("⬇️ Downloading latest binary via GitHub API...")
        release_url = "https://api.github.com/repos/suwei8/GravityBridge-Go/releases/latest"
        headers = {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        
        try:
             # 1. Get Release Info
             resp = requests.get(release_url, headers=headers)
             resp.raise_for_status()
             data = resp.json()
             
             # 2. Find Asset URL
             asset_url = None
             for asset in data.get("assets", []):
                 if asset["name"] == "gravity-agent-linux-arm64":
                     asset_url = asset["url"]
                     break
            
             if not asset_url:
                 print("❌ Start failed: Binary 'gravity-agent-linux-arm64' not found in latest release")
                 return
                 
             # 3. Stream Download Asset
             print(f"Fetching asset from {asset_url}...")
             headers["Accept"] = "application/octet-stream"
             resp_dl = requests.get(asset_url, headers=headers, stream=True)
             
             if resp_dl.status_code == 200:
                 with open("gravity-agent", "wb") as f:
                     shutil.copyfileobj(resp_dl.raw, f)
                 os.chmod("gravity-agent", 0o755)
                 print("✅ Download successful.")
             else:
                 print(f"❌ Failed to download binary asset: {resp_dl.status_code}")
                 return

        except Exception as e:
             print(f"❌ Download Exception: {e}")
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
    parser.add_argument("--action", choices=["check", "restart", "deploy", "debug"], required=True)
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
    elif args.action == "debug":
        if not args.target:
            print("❌ --target is required for debug action")
            return
        debug_agent(args.target, agents)


if __name__ == "__main__":
    main()
