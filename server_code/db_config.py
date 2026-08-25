"""
db_config.py - Anvil Server Module (Titan AI app)
=================================================
Titan Flow Control — Titan AI chat assistant
Centralized database connection management.

Uses Anvil Secrets for credentials (Settings → Secrets).

Required Secrets:
    - NEXUS_SERVER
    - NEXUS_DATABASE
    - NEXUS_USERNAME
    - NEXUS_PASSWORD

Note: NO Anthropic key in this app — all AI work happens in
titan-ai-service on Hermes. This app only reads/writes the
tai_chat_* tables.
"""

import pymssql
import anvil.secrets


def get_nexus_connection():
  """Returns a pymssql connection to the Nexus database."""
  return pymssql.connect(
    server=anvil.secrets.get_secret('NEXUS_SERVER'),
    user=anvil.secrets.get_secret('NEXUS_USERNAME'),
    password=anvil.secrets.get_secret('NEXUS_PASSWORD'),
    database=anvil.secrets.get_secret('NEXUS_DATABASE'),
    port='1433',
    as_dict=True
  )


def test_nexus_connection():
  """Tests the Nexus database connection."""
  try:
    conn = get_nexus_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 AS test")
    cursor.fetchone()
    conn.close()
    print("✓ Nexus connection successful")
    return True
  except Exception as e:
    print(f"✗ Nexus connection failed: {e}")
    raise
