import paramiko
import os

host = '10.128.32.118'
port = 22
username = 'root'
password = 'cderfv34'
remote_dir = '/var/www/standup_bot'

files_to_upload = ['bot.py', '.env']

print(f"Connecting to {host}...")
s = paramiko.SSHClient()
s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
s.connect(host, port, username, password, timeout=30)
print("Connected!")

sftp = s.open_sftp()
local_dir = os.path.dirname(os.path.abspath(__file__))

for fname in files_to_upload:
    local_path = os.path.join(local_dir, fname)
    remote_path = f"{remote_dir}/{fname}"
    print(f"Uploading {fname}...")
    sftp.put(local_path, remote_path)
    print(f"  -> {remote_path} OK")

sftp.close()

print("\nRestarting standup_bot service...")
_, out, err = s.exec_command("systemctl restart standup_bot.service")
out.channel.recv_exit_status()
print("Service restarted.")

print("\nChecking service status...")
_, out, err = s.exec_command("systemctl status standup_bot.service --no-pager -l")
print(out.read().decode('utf-8', errors='replace'))

s.close()
print("\nDone!")
