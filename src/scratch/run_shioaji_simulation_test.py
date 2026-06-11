import os
import sys
import time

# 載入專案路徑
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.services.credential_manager import load_credentials

def main():
    print("==========================================================")
    print(" 🧪 永豐金證券 Shioaji API 模擬模式下單測試 (測試報告) 🧪")
    print("==========================================================")
    
    try:
        import shioaji as sj
    except ImportError:
        print("❌ 錯誤：請確保環境中已安裝 shioaji")
        return

    # 1. 載入憑證
    try:
        credentials = load_credentials()
        broker_creds = credentials.get("brokerCredentials", {})
        
        api_key = broker_creds.get("apiId")
        secret_key = broker_creds.get("apiSecret")
        password = broker_creds.get("password")
        cert_path = broker_creds.get("certificatePath")
        person_id = broker_creds.get("personId")
        
        if not api_key or not secret_key:
            raise ValueError("安全憑證中缺少 apiId 或 apiSecret，無法登入")
    except Exception as e:
        print(f"❌ 載入憑證失敗: {e}")
        return

    # 2. 登入 Shioaji 模擬模式
    print("\n[步驟 1] 正在登入永豐金 API 模擬模式 (simulation=True)...")
    try:
        api = sj.Shioaji(simulation=True)
        api.login(api_key=api_key, secret_key=secret_key)
        print("✅ 模擬模式登入成功！")
    except Exception as e:
        print(f"❌ 模擬模式登入失敗: {e}")
        return

    # 3. 啟用憑證
    print("\n[步驟 2] 正在啟用 CA 下單憑證...")
    if cert_path and password and person_id:
        if os.path.exists(cert_path):
            try:
                api.activate_ca(
                    ca_path=cert_path,
                    ca_passwd=password,
                    person_id=person_id
                )
                print("✅ CA 憑證載入與啟用成功！")
            except Exception as e:
                print(f"❌ 憑證啟用失敗: {e}")
                print("💡 註：在模擬模式中，部分帳號可能可以免憑證下單，但為完成正式測試，請確認憑證資訊無誤。")
        else:
            print(f"⚠️ 憑證檔案不存在於 {cert_path}，將嘗試免憑證模擬下單。")
    else:
        print("⚠️ 憑證欄位不完整，將嘗試免憑證模擬下單。")

    # 4. 證券下單測試
    stock_account = api.stock_account
    if stock_account:
        print("\n[步驟 3] 正在進行【證券】下單測試...")
        try:
            # 查詢測試標的，使用台積電 (2330)
            contract = api.Contracts.Stocks["2330"]
            
            # 建立證券委託 (買入 1 股零股，價格設為 500 元以利成交)
            order = api.Order(
                action=sj.constant.Action.Buy,
                price=500.0,  # 委託價格
                quantity=1,   # 1 股（零股）
                price_type=sj.constant.StockPriceType.LMT,
                order_type=sj.constant.OrderType.ROD,
                order_lot=sj.constant.StockOrderLot.IntradayOdd,  # 零股
                account=stock_account
            )
            
            print(f"👉 送出證券委託: 買入 2330 台積電 1 股，價格 500 元...")
            trade = api.place_order(contract, order)
            print(f"✅ 證券下單成功！委託狀態: {trade.status.status} | 委託單號: {trade.status.id}")
        except Exception as e:
            print(f"❌ 證券下單測試失敗: {e}")
    else:
        print("\nℹ️ 偵測到帳戶清單中無證券帳戶，跳過證券測試。")

    # 依照規定，證券與期貨下單測試需間隔 1 秒以上
    print("\n⏳ 依據規範，間隔 2 秒後進行期貨測試...")
    time.sleep(2)

    # 5. 期貨下單測試
    futopt_account = api.futopt_account
    if futopt_account:
        print("\n[步驟 4] 正在進行【期貨】下單測試...")
        try:
            # 查詢期貨台指近月合約 (FITX)
            contract = None
            try:
                contract = api.Contracts.Futures.TXF.near
            except Exception:
                pass
                
            if not contract:
                raise ValueError("無法取得台指期貨近月合約資訊")
                
            # 建立期貨委託 (買入 1 口，價格設為 10000 點)
            order = api.Order(
                action=sj.constant.Action.Buy,
                price=10000.0,
                quantity=1,
                price_type=sj.constant.FuturesPriceType.LMT,
                order_type=sj.constant.OrderType.ROD,
                account=futopt_account
            )
            
            print(f"👉 送出期貨委託: 買入台指期貨近月 1 口，價格 10000 點...")
            trade = api.place_order(contract, order)
            print(f"✅ 期貨下單成功！委託狀態: {trade.status.status} | 委託單號: {trade.status.id}")
        except Exception as e:
            print(f"❌ 期貨下單測試失敗: {e}")
    else:
        print("\nℹ️ 偵測到帳戶清單中無期貨/選擇權帳戶，跳過期貨測試。")

    print("\n==========================================================")
    print(" 🎉 模擬測試指令執行完畢！")
    print(" 請登入永豐金 API 開發者網站，確認您的測試報告狀態是否均已轉為「已測試」！")
    print("==========================================================")

if __name__ == "__main__":
    main()
