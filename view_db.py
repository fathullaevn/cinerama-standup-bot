"""View current database contents."""
import json, sys
import psycopg2
sys.stdout.reconfigure(encoding='utf-8')

DATABASE_URL = "postgres://u1sii61d1tr5vq:p8a4baffbb572c6d65e25f30c94ca82825707d6dc53d583e5c3d3dfa86522aca5@cd62ai72qd7d5j.cluster-czrs8kj4isg7.us-east-1.rds.amazonaws.com:5432/d2itgfsjjjvdd6"
DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

conn = psycopg2.connect(DATABASE_URL, sslmode="require")
cur = conn.cursor()
cur.execute("SELECT data FROM bot_data WHERE id = 1")
row = cur.fetchone()
cur.close()
conn.close()

data = row[0]
print(json.dumps(data, indent=2, ensure_ascii=False)[:3000])
print(f"\n\n=== Total keys: {list(data.keys())} ===")
print(f"=== Employees: {data.get('employees', {})} ===")
print(f"=== User map entries: {len(data.get('user_map', {}))} ===")
print(f"=== Pinging paused: {data.get('pinging_paused')} ===")

# Show today's data
from datetime import datetime
today = datetime.now().strftime("%Y-%m-%d")
today_data = data.get(today, {})
if today_data:
    replies = today_data.get("replies", {})
    print(f"\n=== Today ({today}): {len(replies)} replies ===")
    for uid, reply in replies.items():
        print(f"  - {reply.get('name')} (@{reply.get('username')}) at {reply.get('time')}")
else:
    print(f"\n=== No data for today ({today}) ===")
