import os
import pytest
from utils.crypto import encrypt_token, decrypt_token

def test_encrypt_decrypt_cycle():
    """Verifica que un token encriptado sea recuperable exactamente igual."""
    original_token = "ya29.a0AfB_byD1234567890"
    encrypted = encrypt_token(original_token)
    
    assert encrypted != original_token
    assert len(encrypted) > len(original_token)
    
    decrypted = decrypt_token(encrypted)
    assert decrypted == original_token

def test_encrypt_empty_values():
    """Asegura que valores vacíos se manejen sin errores."""
    assert encrypt_token(None) is None
    assert decrypt_token(None) is None
    assert encrypt_token("") == "" # Según la implementación actual
