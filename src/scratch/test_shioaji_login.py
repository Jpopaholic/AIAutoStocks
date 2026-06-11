import os
import sys

# 載入專案路徑
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.services.broker_connector import _get_shioaji_api

def main():
    print("==========================================================")
    print(" ⚡ 永豐金證券 Shioaji API 連線與憑證啟用實體測試 ⚡")
    print("==========================================================")
    
    # 強制關閉 Shioaji SDK 的模擬模式（測試真實帳號登入與憑證啟用）
    # 因為 Shioaji 的登入測試需要連線至真實環境或模擬環境
    try:
        api = _get_shioaji_api()
        
        print("\n🎉 連線與憑證啟用測試成功！")
        print("-" * 50)
        print(f"👉 帳戶清單: {api.list_accounts()}")
        print(f"👉 目前使用帳戶: {api.stock_account}")
        
        # 測試查詢功能
        print("\n🔍 測試合約查詢...")
        contract = api.Contracts.Stocks["2330"]
        if contract:
            print(f"✅ 成功查詢股票合約: {contract.code} {contract.name} (交易所: {contract.exchange})")
            print("✨ 永豐金 API 功能測試已完全通過！")
        else:
            print("❌ 查詢合約失敗：無法取得股票 2330 的資訊。")
            
    except Exception as e:
        print(f"\n❌ 連線測試失敗！錯誤訊息:\n{str(e)}")
        print("\n💡 提示：")
        print("1. 請確認 credentials.json 中的 apiId, apiSecret, personId 與憑證密碼是否正確。")
        print("2. 請確認您的 .pfx 憑證檔案確實已放到指定的 certificatePath 路徑。")

if __name__ == "__main__":
    main()
