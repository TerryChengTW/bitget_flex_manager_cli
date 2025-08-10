import base64
import hashlib
import hmac
import time
import requests
import json
import os


# ===== 配置管理 =====
# 全域變數保存用戶選擇的配置文件路徑
_selected_config_path = None

def load_config(allow_file_selection=False):
    """載入配置檔案"""
    global _selected_config_path
    
    if allow_file_selection:
        try:
            import tkinter as tk
            from tkinter import filedialog
            import platform
            
            # 創建主視窗
            root = tk.Tk()
            root.title("選擇配置文件")
            
            # 根據作業系統選擇不同的視窗處理方式
            if platform.system() == 'Darwin':  # macOS
                # macOS 需要可見的小視窗來確保對話框正常運作
                root.geometry("1x1+200+200")  # 設定很小的視窗大小和位置
                root.attributes('-topmost', True)  # 讓對話框置頂
                root.lift()  # 提升到前台
                root.update()  # 確保視窗更新
            else:  # Windows 和 Linux
                # 其他系統可以隱藏主視窗
                root.withdraw()
                root.attributes('-topmost', True)
                root.lift()
                root.focus_force()
            
            # 設置預設目錄
            initial_dir = os.path.dirname(__file__)
            
            # 打開文件選擇對話框
            print("[提示] 請在彈出的對話框中選擇配置文件...")
            
            config_path = filedialog.askopenfilename(
                title="選擇 Bitget API 配置文件",
                initialdir=initial_dir,
                filetypes=[
                    ("JSON files", "*.json"),
                    ("All files", "*.*")
                ]
            )
            
            # 關閉主視窗
            root.quit()
            root.destroy()
            
            if not config_path:  # 用戶取消選擇
                print("[取消] 用戶取消文件選擇")
                return None
            
            # 保存用戶選擇的路徑
            _selected_config_path = config_path
            
        except Exception as e:
            print(f"[錯誤] 文件對話框無法使用: {e}")
            return None
    else:
        # 如果用戶已經選擇過配置文件，使用選擇的路徑
        if _selected_config_path:
            config_path = _selected_config_path
        else:
            # 沒有選擇過配置文件
            print("[錯誤] 尚未選擇配置文件")
            return None
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        if allow_file_selection:
            print(f"[成功] 載入配置文件: {config_path}")
        return config
    except FileNotFoundError:
        if allow_file_selection:
            print(f"[錯誤] 找不到配置文件: {config_path}")
        return None
    except json.JSONDecodeError:
        print(f"[錯誤] 配置文件格式錯誤: {config_path}")
        return None
    except Exception as e:
        print(f"[錯誤] 載入配置文件失敗: {e}")
        return None


def save_config(config):
    """保存配置檔案"""
    global _selected_config_path
    
    # 如果用戶已經選擇過配置文件，保存到選擇的路徑
    if _selected_config_path:
        config_path = _selected_config_path
    else:
        # 沒有選擇過配置文件
        print("[錯誤] 尚未選擇配置文件，無法保存")
        return False
    
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    
    print(f"[保存] 配置已保存到: {config_path}")
    return True


def get_current_account_config():
    """獲取當前帳戶配置 (預設使用main)"""
    config = load_config()
    if not config:
        return None
    return config.get('accounts', {}).get('main')


def get_account_config(account_key):
    """獲取指定帳戶配置"""
    config = load_config()
    if not config:
        return None
    return config.get('accounts', {}).get(account_key)


# ===== API 基礎功能 =====
def generate_signature(secret, timestamp, method, request_path, body=''):
    """生成 Bitget API 簽名"""
    message = str(timestamp) + method + request_path + body
    signature = base64.b64encode(
        hmac.new(secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha256).digest()
    ).decode('utf-8')
    return signature


def make_bitget_request(method, endpoint, body='', account_key=None):
    """發送 Bitget API 請求"""
    if account_key:
        account_config = get_account_config(account_key)
    else:
        account_config = get_current_account_config()
        
    if not account_config:
        return {'code': 'ERROR', 'msg': 'Account config not found'}
    
    base_url = 'https://api.bitget.com'
    timestamp = str(int(time.time() * 1000))
    request_path = endpoint
    
    signature = generate_signature(account_config['secret'], timestamp, method, request_path, body)
    
    headers = {
        'ACCESS-KEY': account_config['apikey'],
        'ACCESS-SIGN': signature,
        'ACCESS-TIMESTAMP': timestamp,
        'ACCESS-PASSPHRASE': account_config['passphrase'],
        'Content-Type': 'application/json'
    }
    
    if method == 'GET':
        response = requests.get(base_url + endpoint, headers=headers)
    elif method == 'POST':
        response = requests.post(base_url + endpoint, headers=headers, data=body)
    
    return response.json()


# ===== 虛擬子帳戶 API =====
def get_virtual_subaccount_list():
    """獲取虛擬子帳戶列表"""
    return make_bitget_request('GET', '/api/v2/user/virtual-subaccount-list')


def create_virtual_subaccount_batch(sub_account_list):
    """批量創建虛擬子帳戶"""
    body = json.dumps({
        "subAccountList": sub_account_list
    })
    return make_bitget_request('POST', '/api/v2/user/create-virtual-subaccount', body)


def create_subaccount_apikey(sub_account_uid, passphrase, label, permissions=None, ip_list=None):
    """創建虛擬子帳戶 API Key"""
    if permissions is None:
        permissions = ["transfer"]
    
    body_data = {
        "subAccountUid": sub_account_uid,
        "passphrase": passphrase,
        "label": label,
        "permList": permissions
    }
    
    if ip_list:
        body_data["ipList"] = ip_list
    
    body = json.dumps(body_data)
    return make_bitget_request('POST', '/api/v2/user/create-virtual-subaccount-apikey', body)


# ===== 子母帳戶劃轉 API =====
def subaccount_transfer(from_user_id, to_user_id, coin, amount, from_type='spot', to_type='spot', 
                       symbol=None, client_oid=None, account_key='main'):
    """子母帳戶資產劃轉 (統一API，支援所有劃轉類型)
    
    支援的劃轉類型：
    - 母帳戶轉子帳戶 (僅母帳戶APIKey有權限)
    - 子帳戶轉母帳戶 (僅母帳戶APIKey有權限)
    - 子帳戶轉子帳戶 (僅母帳戶APIKey有權限)
    - 子帳戶內部劃轉 (僅母帳戶APIKey有權限)
    
    Args:
        from_user_id: 轉出帳戶UID
        to_user_id: 轉入帳戶UID  
        coin: 幣種，如 'USDT'
        amount: 劃轉金額
        from_type: 轉出業務線類型 ('spot', 'coin_futures', 'usdt_futures', 'usdc_futures', 'crossed_margin', 'isolated_margin')
        to_type: 轉入業務線類型 ('spot', 'coin_futures', 'usdt_futures', 'usdc_futures', 'crossed_margin', 'isolated_margin')
        symbol: 交易對名稱 (涉及逐倉槓桿時需要)
        client_oid: 客戶自定義ID (可選)
        account_key: 指定使用的帳戶配置key (必須是主帳戶)
    """
    endpoint = '/api/v2/spot/wallet/subaccount-transfer'
    
    body_data = {
        'fromType': from_type,
        'toType': to_type,
        'amount': str(amount),
        'coin': coin,
        'fromUserId': str(from_user_id),
        'toUserId': str(to_user_id)
    }
    
    # 可選參數
    if symbol:
        body_data['symbol'] = symbol
    if client_oid:
        body_data['clientOid'] = client_oid
    
    body = json.dumps(body_data)
    return make_bitget_request('POST', endpoint, body, account_key=account_key)


def transfer_to_subaccount(coin, amount, sub_account_uid, main_account_uid=None, 
                          from_type='spot', to_type='spot', account_key='main'):
    """主帳戶轉入虛擬子帳戶 (便利函數)
    
    Args:
        coin: 幣種，如 'USDT'
        amount: 轉帳金額
        sub_account_uid: 子帳戶UID
        main_account_uid: 主帳戶UID (如果不提供，將從配置中獲取)
        from_type: 轉出業務線類型，默認 'spot' 現貨
        to_type: 轉入業務線類型，默認 'spot' 現貨
        account_key: 指定使用的帳戶配置key (主帳戶)
    """
    # 如果沒提供主帳戶UID，嘗試從配置中獲取
    if not main_account_uid:
        config = load_config()
        if config:
            main_config = config.get('accounts', {}).get(account_key, {})
            main_account_uid = main_config.get('uuid')
        
        if not main_account_uid:
            # 如果配置中沒有UUID，可能需要特殊處理或報錯
            # 暫時使用佔位符，實際使用時需要正確的主帳戶UID
            return {'code': 'ERROR', 'msg': 'Main account UID not found in config'}
    
    return subaccount_transfer(
        from_user_id=main_account_uid,
        to_user_id=sub_account_uid,
        coin=coin,
        amount=amount,
        from_type=from_type,
        to_type=to_type,
        account_key=account_key
    )


def transfer_to_main_account(coin, amount, sub_account_uid, main_account_uid=None,
                           from_type='spot', to_type='spot', account_key='main'):
    """虛擬子帳戶轉回主帳戶 (便利函數)
    
    Args:
        coin: 幣種，如 'USDT'
        amount: 轉帳金額
        sub_account_uid: 子帳戶UID
        main_account_uid: 主帳戶UID (如果不提供，將從配置中獲取)
        from_type: 轉出業務線類型，默認 'spot' 現貨
        to_type: 轉入業務線類型，默認 'spot' 現貨
        account_key: 指定使用的帳戶配置key (主帳戶)
    """
    # 如果沒提供主帳戶UID，嘗試從配置中獲取
    if not main_account_uid:
        config = load_config()
        if config:
            main_config = config.get('accounts', {}).get(account_key, {})
            main_account_uid = main_config.get('uuid')
        
        if not main_account_uid:
            return {'code': 'ERROR', 'msg': 'Main account UID not found in config'}
    
    return subaccount_transfer(
        from_user_id=sub_account_uid,
        to_user_id=main_account_uid,
        coin=coin,
        amount=amount,
        from_type=from_type,
        to_type=to_type,
        account_key=account_key
    )


# ===== 理財寶 API =====
def get_savings_assets(period_type='flexible', limit=20, account_key=None):
    """獲取理財寶資產信息
    
    Args:
        period_type: 期限類型 - 'flexible' 活期, 'fixed' 定期
        limit: 查詢條數，默認20，最大100
        account_key: 指定使用的帳戶配置key
    """
    endpoint = f'/api/v2/earn/savings/assets?periodType={period_type}&limit={limit}'
    return make_bitget_request('GET', endpoint, account_key=account_key)


# ===== 理財寶產品查詢 =====
def get_savings_products(coin=None, filter_type='available', account_key=None):
    """獲取理財寶產品列表
    
    Args:
        coin: 指定幣種，如 'USDT'
        filter_type: 篩選條件 ('available', 'held', 'available_and_held', 'all')
        account_key: 指定使用的帳戶配置key
    """
    endpoint = '/api/v2/earn/savings/product'
    
    params = []
    if coin:
        params.append(f'coin={coin}')
    if filter_type:
        params.append(f'filter={filter_type}')
    
    if params:
        endpoint += '?' + '&'.join(params)
        
    return make_bitget_request('GET', endpoint, account_key=account_key)


def get_savings_subscribe_info(product_id, period_type, account_key=None):
    """獲取理財寶申購信息
    
    Args:
        product_id: 產品ID
        period_type: 期限類型 ('flexible' 或 'fixed')
        account_key: 指定使用的帳戶配置key
    """
    endpoint = f'/api/v2/earn/savings/subscribe-info?productId={product_id}&periodType={period_type}'
    return make_bitget_request('GET', endpoint, account_key=account_key)


def savings_subscribe(product_id, period_type, amount, account_key=None):
    """理財寶申購
    
    Args:
        product_id: 產品ID
        period_type: 期限類型 ('flexible' 或 'fixed')
        amount: 申購金額
        account_key: 指定使用的帳戶配置key
    """
    endpoint = '/api/v2/earn/savings/subscribe'
    body = json.dumps({
        'productId': str(product_id),
        'periodType': period_type,
        'amount': str(amount)
    })
    return make_bitget_request('POST', endpoint, body, account_key=account_key)


def get_spot_assets(coin='USDT', account_key=None):
    """獲取現貨資產
    
    Args:
        coin: 幣種，默認 'USDT'
        account_key: 指定使用的帳戶配置key
    """
    endpoint = f'/api/v2/spot/account/assets?coin={coin}'
    return make_bitget_request('GET', endpoint, account_key=account_key)


def get_all_subaccount_assets(id_less_than=None, limit=50, account_key=None):
    """獲取所有子帳戶現貨資產 (僅限主帳戶調用)
    
    Args:
        id_less_than: 游標ID，分頁用，首次請求不傳
        limit: 每頁返回的子帳戶數量，默認10，最大50
        account_key: 指定使用的帳戶配置key (應該是主帳戶)
    """
    endpoint = '/api/v2/spot/account/subaccount-assets'
    
    params = []
    if id_less_than:
        params.append(f'idLessThan={id_less_than}')
    if limit != 10:  # 只在非默認值時添加參數
        params.append(f'limit={limit}')
    
    if params:
        endpoint += '?' + '&'.join(params)
    
    return make_bitget_request('GET', endpoint, account_key=account_key)


def get_account_info(account_key=None):
    """獲取帳戶信息 (包含UID等基本信息)
    
    Args:
        account_key: 指定使用的帳戶配置key
    """
    endpoint = '/api/v2/spot/account/info'
    return make_bitget_request('GET', endpoint, account_key=account_key)


def savings_redeem(product_id, period_type, amount, order_id=None, account_key=None):
    """理財寶贖回
    
    Args:
        product_id: 產品ID
        period_type: 期限類型 ('flexible' 或 'fixed')
        amount: 贖回金額
        order_id: 申購記錄ID（可選）
        account_key: 指定使用的帳戶配置key
    """
    endpoint = '/api/v2/earn/savings/redeem'
    body_data = {
        'productId': str(product_id),
        'periodType': period_type,
        'amount': str(amount)
    }
    if order_id:
        body_data['orderId'] = str(order_id)
    
    body = json.dumps(body_data)
    return make_bitget_request('POST', endpoint, body, account_key=account_key)
