#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
安全憑證加密助手 (Credentials Encryption Helper)
此指令碼協助使用者將明文的 credentials.json 加密成安全憑證檔案 (預設為 credentials.enc)，
以便系統在生產環境/實盤環境下解密使用。
"""

import os
import sys
import json
import shutil

# 確保可以導入專案中的 src 模組
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

try:
    from src.services.credential_manager import encrypt_credentials_file
except ImportError as e:
    print(f"❌ 無法載入憑證管理器模組，請確認您是在專案根目錄下執行此指令碼。\n錯誤訊息: {e}")
    sys.exit(1)

def load_master_key_and_output_path():
    """
    從 config.json, .env 或使用者輸入獲取 MASTER_KEY 與輸出路徑
    """
    master_key = None
    output_path = "credentials.enc"
    
    # 1. 嘗試載入 config.json
    config_path = os.path.join(current_dir, "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
                master_key = config_data.get("MASTER_KEY")
                output_path = config_data.get("CREDENTIALS_FILE_PATH") or output_path
                print("ℹ️ 已從 config.json 偵測到設定值。")
        except Exception as e:
            print(f"⚠️ 讀取 config.json 時發生錯誤: {e}")

    # 2. 如果 config.json 沒有，嘗試載入 .env
    if not master_key:
        env_path = os.path.join(current_dir, ".env")
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("MASTER_KEY="):
                            master_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        elif line.startswith("CREDENTIALS_FILE_PATH="):
                            output_path = line.split("=", 1)[1].strip().strip('"').strip("'")
                if master_key:
                    print("ℹ️ 已從 .env 偵測到 MASTER_KEY。")
            except Exception as e:
                print(f"⚠️ 讀取 .env 時發生錯誤: {e}")

    # 3. 提示輸入
    if not master_key:
        print("\n🔑 系統未在 config.json 或 .env 中偵測到 MASTER_KEY。")
        master_key = input("請輸入您的加密主金鑰 (MASTER_KEY): ").strip()
        if not master_key:
            print("❌ 錯誤：必須提供 MASTER_KEY 才能進行加密。")
            sys.exit(1)
            
    return master_key, output_path

def main():
    print("=" * 60)
    print(" 🔐 永豐證券 & Gemini 安全憑證加密助手 🔐")
    print("=" * 60)
    
    plain_path = os.path.join(current_dir, "credentials.json")
    example_path = os.path.join(current_dir, "credentials.example.json")
    
    # 檢查 credentials.json 是否存在
    if not os.path.exists(plain_path):
        print(f"❌ 未找到 '{plain_path}' 檔案！")
        print(f"💡 請先將 '{example_path}' 複製為 'credentials.json'，")
        print("   填入您的 Gemini API Key 及永豐證券 (Shioaji) 帳密後，再重新執行此指令碼。")
        
        # 幫使用者自動複製一個 credentials.json 作為起點（如果不存在）
        try:
            shutil.copyfile(example_path, plain_path)
            print(f"\n👉 已為您自動產生空白的 '{plain_path}'，請編輯該檔案填入真實金鑰。")
        except Exception as e:
            print(f"⚠️ 無法自動複製模板: {e}")
            
        sys.exit(1)
        
    # 驗證 credentials.json 格式與必要欄位
    try:
        with open(plain_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        if "geminiApiKeys" not in data or not isinstance(data["geminiApiKeys"], list):
            print("⚠️ 警告: credentials.json 缺少 'geminiApiKeys' 欄位或格式不正確（必須為陣列）。")
            
        if "supabase" not in data:
            print("⚠️ 警告: credentials.json 缺少 'supabase' 欄位。")
        else:
            s_creds = data["supabase"]
            if not s_creds.get("url") or not s_creds.get("key"):
                print("⚠️ 警告: 'supabase' 缺少 url 或 key 設定。")
                
        if "discord" not in data:
            print("⚠️ 警告: credentials.json 缺少 'discord' 通知欄位，系統將缺乏通知管道。")
        else:
            d_creds = data["discord"]
            if not d_creds.get("webhookSandbox") or not d_creds.get("webhookLive"):
                print("⚠️ 提示: 'discord' 缺少 webhookSandbox 或 webhookLive 設定，將無法使用 Discord Webhook 通知。")

        if "brokerCredentials" not in data:
            print("⚠️ 警告: credentials.json 缺少 'brokerCredentials' 欄位。")
        else:
            creds = data["brokerCredentials"]
            required = ["apiId", "apiSecret", "password", "certificatePath", "personId"]
            missing = [r for r in required if r not in creds or not creds[r]]
            if missing:
                print(f"⚠️ 提示: 'brokerCredentials' 缺少以下欄位或值為空: {missing}")
                print("   (若目前僅要進行模擬/紙上交易，可先填寫 placeholder 或是留空。)")

    except json.JSONDecodeError as je:
        print(f"❌ 'credentials.json' 不是合法的 JSON 格式: {je}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ 讀取 'credentials.json' 失敗: {e}")
        sys.exit(1)

    # 取得 Master Key 與輸出路徑
    master_key, output_path_rel = load_master_key_and_output_path()
    output_path = os.path.join(current_dir, output_path_rel)
    
    # 進行加密
    print(f"\n⚡ 正在使用 MASTER_KEY 加密 '{plain_path}'...")
    try:
        encrypt_credentials_file(plain_path, output_path, master_key)
        print(f"✨ 加密完成！加密後的憑證已儲存至: {output_path}")
    except Exception as e:
        print(f"❌ 加密失敗: {e}")
        sys.exit(1)
        
    # 安全提示與自動清理選項
    print("\n" + "!" * 60)
    print(" ⚠️ 安全提醒：")
    print(" 加密後的 credentials.enc 才可以安全地提交或部署。")
    print(" 明文的 credentials.json 含有您的敏感帳密，絕對不能外流或提交至 Git！")
    print("!" * 60)
    
    confirm = input("\n❓ 是否立即刪除明文的 credentials.json 以確保安全？ (y/N): ").strip().lower()
    if confirm == "y":
        try:
            os.remove(plain_path)
            print("🧹 已成功刪除明文的 credentials.json。")
        except Exception as e:
            print(f"⚠️ 無法自動刪除 credentials.json，請手動刪除。錯誤: {e}")
    else:
        print("👉 請務必記得手動刪除 credentials.json，或確保它已被加入 .gitignore 中。")

if __name__ == "__main__":
    main()
