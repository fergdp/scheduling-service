import os
from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

# La FERNET_KEY debe ser de 32 bytes y estar codificada en base64.
# Se obtiene del archivo .env
FERNET_KEY = os.getenv("FERNET_KEY")

if not FERNET_KEY:
    raise ValueError("FERNET_KEY not found in environment variables")

cipher_suite = Fernet(FERNET_KEY.encode())

def encrypt_token(token: str) -> str:
    """Encripta un token de texto plano a una cadena de bytes codificada en base64."""
    if token is None:
        return None
    if token == "":
        return ""
    return cipher_suite.encrypt(token.encode()).decode()

def decrypt_token(encrypted_token: str) -> str:
    """Desencripta un token de una cadena de bytes codificada en base64 a texto plano."""
    if encrypted_token is None:
        return None
    if encrypted_token == "":
        return ""
    return cipher_suite.decrypt(encrypted_token.encode()).decode()
