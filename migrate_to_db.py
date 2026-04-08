"""Migrate data.json to PostgreSQL on Heroku."""
import json
import psycopg2
import psycopg2.extras

DATABASE_URL = "postgres://u1sii61d1tr5vq:p8a4baffbb572c6d65e25f30c94ca82825707d6dc53d583e5c3d3dfa86522aca5@cd62ai72qd7d5j.cluster-czrs8kj4isg7.us-east-1.rds.amazonaws.com:5432/d2itgfsjjjvdd6"
DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Load local data
with open("data.json", "r", encoding="utf-8") as f:
    data = json.load(f)

print(f"Loaded {len(data)} keys from data.json")

# Connect and migrate
conn = psycopg2.connect(DATABASE_URL, sslmode="require")
cur = conn.cursor()

cur.execute("""
    CREATE TABLE IF NOT EXISTS bot_data (
        id INTEGER PRIMARY KEY DEFAULT 1,
        data JSONB NOT NULL DEFAULT '{}'::jsonb
    )
""")

cur.execute("DELETE FROM bot_data WHERE id = 1")
cur.execute(
    "INSERT INTO bot_data (id, data) VALUES (1, %s)",
    (psycopg2.extras.Json(data),)
)

conn.commit()

# Verify
cur.execute("SELECT data FROM bot_data WHERE id = 1")
row = cur.fetchone()
print(f"Migrated! DB has {len(row[0])} keys")
print("Keys:", list(row[0].keys()))

cur.close()
conn.close()
print("Done!")
