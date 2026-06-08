# Path: src/services/credential_manager.py
import json
import os
import hashlib
from typing import Dict, Any, Optional
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from src.config import config

# 記憶體內的私有快取，嚴禁寫入本機暫存檔或輸出至日誌
_cached_credentials: Optional[Dict[str, Any]] = None

def _derive_key(master_key: str) -> bytes:
    """
    依據 Master Key 使用 SHA-256 生成 32 位元組 (256-bit) 的對稱加密密鑰
    """
    if not master_key:
        raise ValueError("未提供 Master Key，無法進行密鑰衍生")
    return hashlib.sha256(master_key.encode('utf-8')).digest()

def encrypt_data(data: Dict[str, Any], master_key: str) -> Dict[str, str]:
    """
    加密資料。
    採用 AES-256-GCM 進行對稱加密，輸出包含 iv, tag 與 encryptedData。
    """
    try:
        key_bytes = _derive_key(master_key)
        aesgcm = AESGCM(key_bytes)
        nonce = os.urandom(12)  # GCM 建議 12 位元組

        plaintext = json.dumps(data).encode('utf-8')
        # cryptography 函式庫的 AEAD.encrypt 會回傳 加密內容 + 16位元組驗證標籤 (Tag)
        encrypted_with_tag = aesgcm.encrypt(nonce, plaintext, None)

        tag = encrypted_with_tag[-16:]
        ciphertext = encrypted_with_tag[:-16]

        return {
            "iv": nonce.hex(),
            "tag": tag.hex(),
            "encryptedData": ciphertext.hex()
        }
    except Exception as e:
        raise RuntimeError(f"安全憑證加密失敗: {str(e)}")

def decrypt_data(encrypted_payload: Dict[str, str], master_key: str) -> Dict[str, Any]:
    """
    解密資料。
    採用 AES-256-GCM 進行對稱解密。
    """
    try:
        key_bytes = _derive_key(master_key)
        aesgcm = AESGCM(key_bytes)

        nonce = bytes.fromhex(encrypted_payload["iv"])
        tag = bytes.fromhex(encrypted_payload["tag"])
        ciphertext = bytes.fromhex(encrypted_payload["encryptedData"])

        # 重組加密內容與驗證標籤
        encrypted_with_tag = ciphertext + tag
        plaintext = aesgcm.decrypt(nonce, encrypted_with_tag, None)

        return json.loads(plaintext.decode('utf-8'))
    except Exception as e:
        raise RuntimeError(f"安全憑證解密失敗（可能金鑰錯誤或資料遭損毀）: {str(e)}")

def load_credentials() -> Dict[str, Any]:
    """
    載入並解密安全憑證與金鑰。
    優先自外部加密金鑰檔案載入；若不存在且為沙盒/開發環境，則可嘗試自環境變數載入以供快速測試。
    """
    global _cached_credentials
    if _cached_credentials is not None:
        return _cached_credentials

    file_path = config.credentials.file_path
    master_key = config.credentials.master_key

    # 1. 檢查是否有外部加密檔案
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                encrypted_payload = json.load(f)

            required_fields = ["iv", "tag", "encryptedData"]
            if not all(field in encrypted_payload for field in required_fields):
                raise ValueError("加密檔案格式不正確，缺少 iv, tag 或 encryptedData 欄位")

            decrypted = decrypt_data(encrypted_payload, master_key)

            # 驗證解密後的核心資料欄位
            if "geminiApiKeys" not in decrypted or not isinstance(decrypted["geminiApiKeys"], list):
                raise ValueError("解密後的憑證格式不正確，缺少 geminiApiKeys 陣列")

            _cached_credentials = decrypted
            print(" [安全憑證管理器] 已成功載入並解密外部加密憑證檔案。")
            return _cached_credentials
        except Exception as e:
            print(f" [安全憑證管理器] 解析外部憑證檔案失敗: {str(e)}")
            raise

    # 2. 外部檔案不存在，嘗試退路（環境變數），僅在 Paper Trading 或開發模式下允許
    if config.limits.is_paper_trading or config.env == "development":
        print(" [安全憑證管理器] 未找到外部加密金鑰檔案，正在嘗試從環境變數載入暫時金鑰（僅限沙盒/開發模式）...")

        env_gemini_keys = os.getenv("GEMINI_API_KEYS")
        if not env_gemini_keys:
            raise RuntimeError(f"未找到加密憑證檔案 ({file_path})，且環境變數中亦無 GEMINI_API_KEYS 配置")

        gemini_api_keys = [key.strip() for key in env_gemini_keys.split(",") if key.strip()]
        if not gemini_api_keys:
            raise RuntimeError("環境變數 GEMINI_API_KEYS 為空，無法載入任何金鑰")

        # 模擬的證券商憑證結構
        broker_credentials = {
            "apiId": os.getenv("BROKER_API_ID") or "MOCK_API_ID",
            "apiSecret": os.getenv("BROKER_API_SECRET") or "MOCK_API_SECRET",
            "password": os.getenv("BROKER_PASSWORD") or "MOCK_PASSWORD",
            "certificatePath": os.getenv("BROKER_CERT_PATH") or "MOCK_CERT_PATH"
        }

        _cached_credentials = {
            "geminiApiKeys": gemini_api_keys,
            "brokerCredentials": broker_credentials
        }

        print(f" [安全憑證管理器] 已自環境變數載入 {len(gemini_api_keys)} 組 Gemini API 金鑰（模擬帳戶模式）。")
        return _cached_credentials

    # 生產/真實下單環境下，若缺少外部安全檔案則必須強制拋錯中斷
    raise RuntimeError(f"真實交易環境下必須提供外部加密金鑰檔案: {file_path}")

def clear_cache() -> None:
    """
    清除記憶體快取（測試或重新載入時使用）
    """
    global _cached_credentials
    _cached_credentials = None

def encrypt_credentials_file(plain_json_path: str, output_path: str, master_key: str) -> None:
    """
    輔助方法：手動建立加密金鑰檔案的實用工具。
    """
    try:
        with open(plain_json_path, 'r', encoding='utf-8') as f:
            plain_object = json.load(f)

        encrypted_payload = encrypt_data(plain_object, master_key)

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(encrypted_payload, f, indent=2)

        print(f" [安全憑證管理器] 成功將加密憑證寫入: {output_path}")
    except Exception as e:
        print(f" [安全憑證管理器] 寫入加密憑證檔案失敗: {str(e)}")
        raise
