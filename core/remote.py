import subprocess


def _ssh_cmd(host: str, port: int, user: str, key_file: str, password: str, remote_cmd: str) -> tuple[int, str]:
    ssh_args = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
    if port and port != 22:
        ssh_args += ["-p", str(port)]
    if key_file:
        ssh_args += ["-i", key_file]
    target = f"{user}@{host}" if user else host
    ssh_args += [target, remote_cmd]
    try:
        r = subprocess.run(ssh_args, capture_output=True, text=True, timeout=15)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return -1, "SSH connection timed out"
    except Exception as e:
        return -1, str(e)


def ssh_test_connection(conn: dict) -> dict:
    code, output = _ssh_cmd(
        conn["host"], conn.get("port", 22), conn.get("user", ""),
        conn.get("key_file", ""), conn.get("password", ""),
        "echo ok && openclaw --version 2>/dev/null || echo 'openclaw not found'"
    )
    return {
        "success": code == 0,
        "message": output.strip(),
        "openclaw_installed": "openclaw" in output.lower() and "not found" not in output.lower(),
    }


def gateway_test_connection(conn: dict) -> dict:
    import requests
    url = conn.get("url", "").rstrip("/")
    token = conn.get("token", "")
    password = conn.get("password", "")

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif password:
        headers["Authorization"] = f"Bearer {password}"

    try:
        r = requests.get(f"{url}/", headers=headers, timeout=10, verify=False)
        return {
            "success": r.status_code == 200,
            "message": f"HTTP {r.status_code}",
            "status_code": r.status_code,
        }
    except requests.exceptions.ConnectionError:
        return {"success": False, "message": "Connection refused"}
    except requests.exceptions.Timeout:
        return {"success": False, "message": "Connection timed out"}
    except Exception as e:
        return {"success": False, "message": str(e)[:200]}
