"""Print fresh random values for GM_SECRET_KEY and GM_ENCRYPTION_KEY:  python -m gm.keys"""
import secrets

from cryptography.fernet import Fernet

if __name__ == "__main__":
    print(f"GM_SECRET_KEY={secrets.token_hex(32)}")
    print(f"GM_ENCRYPTION_KEY={Fernet.generate_key().decode()}")
