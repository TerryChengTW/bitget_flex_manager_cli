#!/usr/bin/env python3
import random
import string
import time
import requests
import threading
from concurrent.futures import ThreadPoolExecutor
from bitget_api import (
    get_virtual_subaccount_list, create_virtual_subaccount_batch, create_subaccount_apikey, 
    load_config, save_config, get_savings_products, get_savings_assets, get_spot_assets, 
    savings_subscribe, get_savings_subscribe_info, savings_redeem, transfer_to_subaccount,
    transfer_to_main_account, get_all_subaccount_assets, get_account_info
)
from version_checker import check_for_updates

# 配置參數
TARGET_SUBACCOUNT_COUNT = 20


def safe_float(value):
    """安全轉換為 8 位精度 float，使用 floor 確保轉帳一致性"""
    if value is None or value == '' or value == 0:
        return 0.0
    try:
        import math
        # 使用 floor 到 8 位數，確保顯示的金額就是實際能轉的金額
        return math.floor(float(value) * 100000000) / 100000000
    except:
        return 0.0



def format_amount(value):
    """格式化金額顯示，8 位精度，移除尾隨零"""
    if abs(value) < 1e-8:
        return '0'
    formatted = f"{value:.8f}"
    return formatted.rstrip('0').rstrip('.')

def format_api_amount(value):
    """格式化 API 用金額字串，8 位精度，使用 floor"""
    import math
    # 確保 API 金額也是 floor 到 8 位數
    floored_value = math.floor(float(value) * 100000000) / 100000000
    return f"{floored_value:.8f}".rstrip('0').rstrip('.')


# ===== 並行化與帳戶管理 Classes =====

class ParallelExecutor:
    """並行 API 呼叫執行器
    
    利用 Bitget 限速按 UID 分開計算的特性，
    對所有帳戶同時發起 API 請求以大幅提升效率。
    """
    
    def __init__(self, max_workers: int = 21):
        self.max_workers = max_workers
    
    def execute_for_accounts(self, accounts: dict, func, *args) -> dict:
        """對所有帳戶並行執行指定函數
        
        Args:
            accounts: {account_id: account_info} 字典
            func: 要執行的函數，簽名為 func(account_id, *args)
            *args: 傳給 func 的額外參數
        
        Returns:
            {account_id: result} 字典
        """
        results = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(func, account_id, *args): account_id
                for account_id in accounts
            }
            for future in futures:
                account_id = futures[future]
                try:
                    results[account_id] = future.result()
                except Exception as e:
                    results[account_id] = {'error': str(e)}
        return results
    
    def execute_in_batches(self, tasks: list, batch_size: int = 8, 
                           delay_between_batches: float = 1.0) -> list:
        """分批並行執行任務 (用於共享限速的操作如轉帳)
        
        Args:
            tasks: [(func, args), ...] 任務列表，每個元素為 (函數, 參數tuple)
            batch_size: 每批最大任務數 (預設 8，保守低於 10/s 限速)
            delay_between_batches: 批次間延遲秒數
        
        Returns:
            [(index, result), ...] 結果列表，包含原始索引和結果
        """
        results = []
        
        for batch_start in range(0, len(tasks), batch_size):
            batch_end = min(batch_start + batch_size, len(tasks))
            batch = tasks[batch_start:batch_end]
            
            with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                futures = {}
                for i, (func, args) in enumerate(batch):
                    idx = batch_start + i
                    futures[executor.submit(func, *args)] = idx
                
                for future in futures:
                    idx = futures[future]
                    try:
                        results.append((idx, future.result()))
                    except Exception as e:
                        results.append((idx, {'code': 'ERROR', 'error': str(e)}))
            
            # 批次間延遲 (最後一批不需要)
            if batch_end < len(tasks):
                time.sleep(delay_between_batches)
        
        # 按索引排序返回
        results.sort(key=lambda x: x[0])
        return results


class BalanceParser:
    """API 回傳資料解析器"""
    
    @staticmethod
    def parse_spot_balance(wallet_result: dict) -> tuple:
        """解析現貨錢包餘額 -> (available, frozen)"""
        if wallet_result.get('code') != '00000':
            return 0.0, 0.0
        wallet_data = wallet_result.get('data', [])
        if not wallet_data:
            return 0.0, 0.0
        available = safe_float(wallet_data[0].get('available', '0'))
        frozen = safe_float(wallet_data[0].get('frozen', '0'))
        return available, frozen
    
    @staticmethod
    def parse_savings_holding(savings_result: dict, product_id: str) -> float:
        """解析理財寶持有量"""
        if savings_result.get('code') != '00000':
            return 0.0
        result_list = savings_result.get('data', {}).get('resultList', [])
        for item in result_list:
            if item.get('productId') == product_id:
                return safe_float(item.get('holdAmount', '0'))
        return 0.0
    
    @staticmethod
    def format_account_name(account_type: str, account_id: str) -> str:
        """格式化帳戶名稱"""
        return '主帳戶' if account_type == 'main' else f'子帳戶{account_id}'


class AccountManager:
    """統一帳戶管理器
    
    提供帳戶載入和並行查詢功能。
    """
    
    def __init__(self):
        self.config = load_config()
        self.executor = ParallelExecutor()
        self.parser = BalanceParser()
        self._accounts_cache = None
    
    def get_valid_accounts(self) -> dict:
        """取得所有有效帳戶 (有 API Key)"""
        if self._accounts_cache is not None:
            return self._accounts_cache
            
        if not self.config:
            return {}
        accounts = {}
        for account_id, info in self.config.get('accounts', {}).items():
            if info.get('type') in ['main', 'sub']:
                if info.get('apikey') and info.get('secret'):
                    accounts[account_id] = info
        self._accounts_cache = accounts
        return accounts
    
    def query_all_spot_assets(self, coin: str) -> dict:
        """並行查詢所有帳戶的現貨資產
        
        Args:
            coin: 幣種 (如 'USDT')
        
        Returns:
            {account_id: {'account_info': ..., 'wallet_result': ...}}
        """
        accounts = self.get_valid_accounts()
        if not accounts:
            return {}
        
        def query_single(account_id, coin):
            return {
                'account_info': accounts[account_id],
                'wallet_result': get_spot_assets(coin, account_id)
            }
        
        return self.executor.execute_for_accounts(accounts, query_single, coin)
    
    def query_all_savings_assets(self, coin: str, product_id: str, period_type: str) -> dict:
        """並行查詢所有帳戶的理財資產
        
        Args:
            coin: 幣種
            product_id: 理財產品 ID
            period_type: 期限類型 ('flexible' 或 'fixed')
        
        Returns:
            {account_id: {'account_info': ..., 'subscribe_info': ..., 
                         'savings_result': ..., 'wallet_result': ...}}
        """
        accounts = self.get_valid_accounts()
        if not accounts:
            return {}
        
        def query_single(account_id, coin, product_id, period_type):
            # 單一帳戶內的 3 個 API 也並行
            with ThreadPoolExecutor(max_workers=3) as inner_executor:
                f_subscribe = inner_executor.submit(
                    get_savings_subscribe_info, product_id, period_type, account_id)
                f_savings = inner_executor.submit(
                    get_savings_assets, period_type, 20, account_id)
                f_wallet = inner_executor.submit(
                    get_spot_assets, coin, account_id)
                
                return {
                    'account_info': accounts[account_id],
                    'subscribe_info': f_subscribe.result(),
                    'savings_result': f_savings.result(),
                    'wallet_result': f_wallet.result()
                }
        
        return self.executor.execute_for_accounts(
            accounts, query_single, coin, product_id, period_type)


def generate_subaccount_name():
    """生成8位純英文字母的子帳戶名稱"""
    return ''.join(random.choices(string.ascii_lowercase, k=8))


def get_my_ip():
    """獲取當前外網IP"""
    try:
        response = requests.get('https://api.ipify.org', timeout=10)
        return response.text.strip()
    except Exception as e:
        print(f"[錯誤] 無法獲取IP: {e}")
        return None


def generate_api_passphrase():
    """生成API Key密碼(8-32位英文字母+數字)"""
    chars = string.ascii_letters + string.digits
    return ''.join(random.choices(chars, k=16))


def create_apikeys_for_subaccounts():
    """為所有子帳戶創建API Key"""
    print("\n=== 創建子帳戶API Key ===")
    
    config = load_config()
    if not config:
        print("[錯誤] 無法載入配置文件")
        return False
    
    # 先檢查是否需要創建API Key
    subaccounts_to_create = []
    for account_id, account_info in config.get('accounts', {}).items():
        if account_info.get('type') == 'sub':
            apikey = account_info.get('apikey', '').strip()
            if not apikey or apikey == '':
                subaccounts_to_create.append({
                    'account_id': account_id,
                    'uuid': account_info['uuid']
                })
    
    if not subaccounts_to_create:
        print("[信息] 所有子帳戶都已有API Key，跳過創建")
        return True
    
    # 如果需要創建API Key，才詢問是否綁定IP
    bind_ip = input("是否綁定當前IP?(建議綁定) (Y/n): ").strip().lower()
    bind_ip = bind_ip != 'n'  # 預設是綁定
    
    ip_list = []
    if bind_ip:
        print("[信息] 正在獲取當前IP...")
        current_ip = get_my_ip()
        if current_ip:
            ip_list = [current_ip]
            print(f"[信息] 當前IP: {current_ip}")
        else:
            print("[警告] 無法獲取IP，將不綁定IP")
    
    print(f"[信息] 需要為 {len(subaccounts_to_create)} 個子帳戶創建API Key")
    
    success_count = 0
    for i, sub_info in enumerate(subaccounts_to_create):
        account_id = sub_info['account_id']
        sub_account_uid = sub_info['uuid']
        
        print(f"\n[步驟{i+1}] 為子帳戶 {account_id} (UID: {sub_account_uid}) 創建API Key...")
        
        # 生成密碼和標籤
        passphrase = generate_api_passphrase()
        label = f"auto_sub{account_id}"
        
        # 創建API Key
        result = create_subaccount_apikey(
            sub_account_uid=sub_account_uid,
            passphrase=passphrase,
            label=label,
            permissions=["transfer", "read", "spot_trade"],
            ip_list=ip_list
        )
        
        if result.get('code') == '00000':
            data = result.get('data', {})
            api_key = data.get('subAccountApiKey')
            secret_key = data.get('secretKey')
            
            # 更新配置文件
            config['accounts'][account_id].update({
                'apikey': api_key,
                'secret': secret_key,
                'passphrase': passphrase
            })
            
            print(f"[成功] API Key 創建成功")
            print(f"       API Key: {api_key}")
            print(f"       權限: {data.get('permList', [])}")
            if ip_list:
                print(f"       綁定IP: {data.get('ipList', [])}")
            
            success_count += 1
        else:
            print(f"[失敗] 創建失敗: {result}")
        
        # 限速保護：每次調用後休息0.5秒
        if i < len(subaccounts_to_create) - 1:  # 最後一次不需要等待
            time.sleep(0.5)
    
    # 保存更新後的配置
    save_config(config)
    print(f"\n[完成] 成功為 {success_count}/{len(subaccounts_to_create)} 個子帳戶創建API Key")
    
    return success_count > 0


def ensure_target_subaccounts():
    """確保有指定數量的虛擬子帳戶，不足則創建"""
    print("=== Bitget Flex CLI ===")
    
    # 步驟1: 獲取現有虛擬子帳戶列表
    print("\n[步驟1] 檢查現有虛擬子帳戶...")
    result = get_virtual_subaccount_list()
    
    if result.get('code') != '00000':
        print(f"[錯誤] {result}")
        return False
    
    data = result.get('data', {})
    existing_subaccounts = data.get('subAccountList', [])
    existing_count = len(existing_subaccounts)
    
    print(f"[信息] 目前有 {existing_count} 個虛擬子帳戶")
    
    # 步驟2: 如果不足目標數量，創建缺少的子帳戶
    if existing_count < TARGET_SUBACCOUNT_COUNT:
        needed_count = TARGET_SUBACCOUNT_COUNT - existing_count
        print(f"\n[步驟2] 需要創建 {needed_count} 個子帳戶...")
        
        # 生成要創建的子帳戶名稱列表
        new_subaccounts = []
        for i in range(needed_count):
            sub_name = generate_subaccount_name()
            new_subaccounts.append(sub_name)
            print(f"  [準備] 將創建: {sub_name}")
        
        # 分批創建子帳戶 (每批最多5個，避免API限制)
        BATCH_SIZE = 5
        total_success = 0
        total_failure = 0
        
        for i in range(0, len(new_subaccounts), BATCH_SIZE):
            batch = new_subaccounts[i:i+BATCH_SIZE]
            batch_num = i // BATCH_SIZE + 1
            total_batches = (len(new_subaccounts) + BATCH_SIZE - 1) // BATCH_SIZE
            
            print(f"\n[批次 {batch_num}/{total_batches}] 創建 {len(batch)} 個子帳戶...")
            
            # 批量創建子帳戶
            create_result = create_virtual_subaccount_batch(batch)
            
            if create_result.get('code') == '00000':
                success_list = create_result.get('data', {}).get('successList', [])
                failure_list = create_result.get('data', {}).get('failureList', [])
                
                batch_success = len(success_list)
                batch_failure = len(failure_list)
                
                print(f"  [批次結果] 成功 {batch_success} 個，失敗 {batch_failure} 個")
                
                total_success += batch_success
                total_failure += batch_failure
                
                if failure_list:
                    print("  [失敗列表]:")
                    for fail in failure_list:
                        print(f"    - {fail.get('subaAccountName')}: {fail.get('reason', '未知原因')}")
            else:
                print(f"  [批次錯誤] {create_result}")
                # 即使某批次失敗，也繼續嘗試下一批次
                total_failure += len(batch)
            
            # 批次間延遲，避免頻率限制
            if i + BATCH_SIZE < len(new_subaccounts):
                print("  [等待] 批次間延遲 1 秒...")
                time.sleep(1)
        
        print(f"\n[總結果] 成功創建 {total_success} 個，失敗 {total_failure} 個")
        
        if total_success == 0:
            print("[錯誤] 所有子帳戶創建都失敗")
            return False
        
        # 重新獲取最新的子帳戶列表
        print("\n[步驟3] 重新獲取子帳戶列表...")
        result = get_virtual_subaccount_list()
        if result.get('code') != '00000':
            return False
        
        data = result.get('data', {})
        existing_subaccounts = data.get('subAccountList', [])
    
    # 檢查是否需要更新配置文件
    print(f"\n[步驟4] 檢查配置文件...")
    needs_update = check_config_needs_update(existing_subaccounts)
    
    if needs_update:
        print("[信息] 配置需要更新...")
        return update_config_with_subaccounts(existing_subaccounts)
    else:
        print("[信息] 配置已是最新，跳過更新")
        return True


def check_config_needs_update(subaccounts):
    """檢查配置文件是否需要更新"""
    config = load_config()
    if not config:
        return True  # 無配置文件，需要更新
    
    # 按 UUID 從小到大排序
    subaccounts_sorted = sorted(subaccounts[:TARGET_SUBACCOUNT_COUNT], key=lambda x: int(x.get('subAccountUid')))
    
    # 檢查數量是否一致
    existing_sub_accounts = {k: v for k, v in config.get('accounts', {}).items() 
                            if v.get('type') == 'sub'}
    
    if len(existing_sub_accounts) != len(subaccounts_sorted):
        return True  # 數量不一致，需要更新
    
    # 檢查UUID順序是否一致
    for i, sub in enumerate(subaccounts_sorted):
        account_id = str(i + 1)
        uid = sub.get('subAccountUid')
        
        if (account_id not in existing_sub_accounts or 
            existing_sub_accounts[account_id].get('uuid') != uid):
            return True  # UUID順序不一致，需要更新
    
    return False  # 配置已是最新


def update_config_with_subaccounts(subaccounts):
    """將子帳戶信息更新到配置文件"""
    config = load_config()
    if not config:
        print("[錯誤] 無法載入配置文件")
        return False
    
    # 建立現有 UUID 到配置的映射，保留 API Key 信息
    existing_uuid_to_config = {}
    for account_id, account_info in config.get('accounts', {}).items():
        if account_info.get('type') == 'sub' and account_info.get('uuid'):
            existing_uuid_to_config[account_info['uuid']] = {
                'apikey': account_info.get('apikey', ''),
                'secret': account_info.get('secret', ''),
                'passphrase': account_info.get('passphrase', '')
            }
    
    # 按 UUID 從小到大排序，然後對應到 1-N
    subaccounts_sorted = sorted(subaccounts[:TARGET_SUBACCOUNT_COUNT], key=lambda x: int(x.get('subAccountUid')))
    
    for i, sub in enumerate(subaccounts_sorted):
        account_id = str(i + 1)  # 1, 2, 3, ..., N
        uid = sub.get('subAccountUid')
        
        # 如果是現有的 UUID，保留原有的 API Key 配置
        if uid in existing_uuid_to_config:
            existing_config = existing_uuid_to_config[uid]
            config['accounts'][account_id] = {
                "type": "sub",
                "uuid": uid,
                "apikey": existing_config['apikey'],
                "secret": existing_config['secret'], 
                "passphrase": existing_config['passphrase']
            }
            print(f"  [記錄] {account_id}: {uid} (保留現有API Key)")
        else:
            # 新的子帳戶，設定空的 API Key 配置
            config['accounts'][account_id] = {
                "type": "sub",
                "uuid": uid,
                "apikey": "",
                "secret": "", 
                "passphrase": ""
            }
            print(f"  [記錄] {account_id}: {uid} (新子帳戶)")
    
    # 保存更新後的配置
    save_config(config)
    print(f"\n[完成] 已記錄 {len(subaccounts_sorted)} 個子帳戶到配置文件")
    
    return True


def savings_management_workflow():
    """理財寶管理主流程"""
    print("\n=== 理財寶管理工具 ===")
    
    # 步驟0: 讓用戶輸入選擇幣種
    coin = input("請輸入要管理的幣種 (例如: USDT, BTC, ETH): ").strip().upper()
    if not coin:
        print("[錯誤] 幣種不能為空")
        return False
        
    print(f"[信息] 選擇的幣種: {coin}")
    print("=" * 50)
    
    # 步驟1: 查詢理財寶產品列表並讓用戶選擇
    selected_product = step1_query_savings_products(coin)
    if not selected_product:
        return False
    
    # 步驟2: 查詢每個帳戶理財寶資產狀況
    account_status = step2_query_current_assets(coin, selected_product)
    if not account_status:
        return False
    
    # 步驟3: 用戶選擇申購策略
    result = step3_user_selection(coin, selected_product, account_status)
    if result is None or result[0] is None:  # 用戶取消或錯誤
        return False
    operations, subscribe_precision = result
    
    # 如果沒有需要執行的操作，直接成功返回
    if not operations:
        return True
    
    # 步驟4: 執行申購操作
    if not step4_execute_operations(coin, selected_product, operations, subscribe_precision):
        return False
    
    # 步驟5: 再次查詢並顯示最終狀況
    if not step5_final_query(coin, selected_product, account_status):
        return False
    
    print("\n" + "=" * 50)
    print("[完成] 理財寶管理流程執行完成")
    return True


def step1_query_savings_products(coin):
    """步驟1: 查詢理財寶產品列表"""
    print(f"\n=== 步驟1: 查詢 {coin} 理財寶產品列表 ===")
    
    # 使用主帳戶查詢產品列表（所有帳戶看到的產品都一樣）
    result = get_savings_products(coin=coin, filter_type='available', account_key='main')
    
    if result.get('code') != '00000':
        print(f"[錯誤] 查詢產品失敗: {result}")
        return False
    
    products = result.get('data', [])
    if not products:
        print(f"[錯誤] 沒有找到 {coin} 的可申購理財寶產品")
        return False
    
    print(f"[信息] 找到 {len(products)} 個可申購產品:")
    
    # 顯示所有產品信息
    for i, product in enumerate(products):
        product_id = product.get('productId')
        period_type = product.get('periodType')
        period = product.get('period', '')
        apy_type = product.get('apyType')
        apy_list = product.get('apyList', [])
        product_level = product.get('productLevel', 'normal')
        
        period_text = "活期" if period_type == 'flexible' else f"{period}天定期"
        level_text = f" ({product_level})" if product_level != 'normal' else ""
        
        print(f"  [{i+1}] {period_text}產品{level_text}")
        print(f"      產品ID: {product_id}")
        print(f"      利率類型: {apy_type}")
        
        # 顯示利率階梯
        for j, apy in enumerate(apy_list):
            min_val = apy.get('minStepVal', '0')
            max_val = apy.get('maxStepVal', '0')
            current_apy = apy.get('currentApy', '0')
            
            if safe_float(max_val) >= 120000000:  # 很大的數字表示無上限
                print(f"      - 階梯{j+1}: {min_val}+ {coin} → {current_apy}% 年化")
            else:
                print(f"      - 階梯{j+1}: {min_val}-{max_val} {coin} → {current_apy}% 年化")
        
        print()
    
    # 讓用戶選擇產品
    while True:
        try:
            choice = input(f"請選擇產品 (1-{len(products)}): ").strip()
            choice_num = int(choice)
            if 1 <= choice_num <= len(products):
                selected_product = products[choice_num - 1]
                break
            else:
                print(f"[錯誤] 請輸入 1-{len(products)} 之間的數字")
        except ValueError:
            print("[錯誤] 請輸入有效的數字")
        except KeyboardInterrupt:
            print("\n[取消] 用戶取消操作")
            return False
    
    product_type = "活期" if selected_product.get('periodType') == 'flexible' else f"{selected_product.get('period')}天定期"
    print(f"[選擇] 已選擇 {product_type}產品 (ID: {selected_product.get('productId')})")
    
    return selected_product


def step2_query_current_assets(coin, selected_product):
    """步驟2: 查詢每個帳戶的理財寶資產狀況 (並行查詢)"""
    print(f"\n=== 步驟2: 查詢所有帳戶理財寶資產狀況 ===")
    
    product_id = selected_product.get('productId')
    period_type = selected_product.get('periodType')
    product_name = "活期" if period_type == 'flexible' else f"{selected_product.get('period')}天定期"
    
    print(f"[產品信息] {product_name}產品 (ID: {product_id})")
    
    # 使用 AccountManager 並行查詢
    manager = AccountManager()
    accounts = manager.get_valid_accounts()
    
    if not accounts:
        print("[錯誤] 沒有找到有效的帳戶配置")
        return False
    
    print(f"[信息] 正在並行查詢 {len(accounts)} 個帳戶的狀況...")
    
    # 並行查詢所有帳戶的理財資產
    account_status = manager.query_all_savings_assets(coin, product_id, period_type)
    
    if not account_status:
        print("[錯誤] 查詢失敗")
        return False
    
    print(f"[信息] 資產查詢完成")
    
    return account_status


def step3_user_selection(coin, selected_product, account_status):
    """步驟3: 用戶選擇申購策略"""
    print(f"\n=== 步驟3: 選擇申購策略 ===")
    
    # 解析產品階梯信息
    apy_list = selected_product.get('apyList', [])
    if not apy_list:
        print("[錯誤] 無法獲取產品階梯信息")
        return None
    
    # 獲取最小申購金額和申購精度 (從任一帳戶的 subscribe_info 中獲取)
    min_purchase_amount = 0.0  # 默認值
    subscribe_precision = 10  # 默認精度
    
    for account_id, status in account_status.items():
        subscribe_info = status.get('subscribe_info', {})
        
        if subscribe_info.get('code') == '00000' and subscribe_info.get('data'):
            data = subscribe_info.get('data', {})
            single_min_amount = data.get('singleMinAmount')
            precision = data.get('subscribePrecision')
            
            
            if single_min_amount:
                min_purchase_amount = safe_float(single_min_amount)
            if precision:
                subscribe_precision = int(precision)
            break
    
    print(f"[產品申購限制] 最小申購金額: {format_amount(min_purchase_amount)} {coin}, 精度: {subscribe_precision}")
    
    # 顯示階梯信息
    print(f"\n[產品階梯信息]")
    tier1_limit = 0.0
    
    for i, apy in enumerate(apy_list):
        min_val = apy.get('minStepVal', '0')
        max_val = apy.get('maxStepVal', '0')
        current_apy = apy.get('currentApy', '0')
        if i == 0:  # 第一階梯
            tier1_limit = safe_float(max_val)
        if safe_float(max_val) >= 120000000:
            print(f"  階梯{i+1}: {min_val}+ {coin} → {current_apy}% 年化")
        else:
            print(f"  階梯{i+1}: {min_val}-{max_val} {coin} → {current_apy}% 年化")
    
    # 顯示當前帳戶狀況總覽
    print(f"\n[帳戶狀況總覽]")
    valid_accounts = []
    
    for account_id, status in account_status.items():
        account_type = status['account_info'].get('type')
        
        # 獲取個人持有量
        personal_holding = 0.0
        savings_result = status.get('savings_result', {})
        if savings_result.get('code') == '00000':
            result_list = savings_result.get('data', {}).get('resultList', [])
            for item in result_list:
                if item.get('productId') == selected_product.get('productId'):
                    personal_holding = safe_float(item.get('holdAmount', '0'))
                    break
        
        # 獲取錢包餘額
        wallet_available = 0.0
        wallet_result = status.get('wallet_result', {})
        if wallet_result.get('code') == '00000' and wallet_result.get('data'):
            wallet_data = wallet_result.get('data', [])
            if wallet_data:
                wallet_available = safe_float(wallet_data[0].get('available', '0'))
        
        # 計算到第一階梯上限的空間
        space_to_tier1 = max(0.0, tier1_limit - personal_holding)
        
        account_name = f"{'主帳戶' if account_type == 'main' else f'子帳戶{account_id}'}"
        print(f"  {account_name}: 持有={format_amount(personal_holding)}, 錢包={format_amount(wallet_available)}, 到{format_amount(tier1_limit)}還可存={format_amount(space_to_tier1)}")
        
        valid_accounts.append({
            'id': account_id,
            'name': account_name,
            'type': account_type,
            'holding': personal_holding,
            'wallet': wallet_available,
            'space_to_tier1': space_to_tier1
        })
    
    # 用戶選擇帳戶
    print(f"\n[選擇要操作的帳戶]")
    print("0. 全部帳戶")
    for i, acc in enumerate(valid_accounts):
        print(f"{i+1}. {acc['name']}")
    
    try:
        choice = input("請選擇 (0-{0}): ".format(len(valid_accounts))).strip()
        choice_num = int(choice)
        
        if choice_num == 0:
            selected_accounts = valid_accounts
            print("[選擇] 全部帳戶")
        elif 1 <= choice_num <= len(valid_accounts):
            selected_accounts = [valid_accounts[choice_num - 1]]
            print(f"[選擇] {valid_accounts[choice_num - 1]['name']}")
        else:
            print("[錯誤] 無效選擇")
            return None
    except (ValueError, KeyboardInterrupt):
        print("[取消] 用戶取消操作")
        return None
    
    # 用戶選擇操作類型
    print(f"\n[選擇操作類型]")
    print(f"1. 存入到填滿{tier1_limit} (第一階梯上限)")
    print(f"2. 取出到剩{tier1_limit} (保留第一階梯上限)")
    print("3. 全部取出")
    
    try:
        op_choice = input("請選擇操作 (1-3): ").strip()
        if op_choice not in ['1', '2', '3']:
            print("[錯誤] 無效選擇")
            return None
    except KeyboardInterrupt:
        print("[取消] 用戶取消操作")
        return None
    
    # 計算操作計劃
    operations = []
    for account in selected_accounts:
        if op_choice == '1':  # 存入到填滿第一階梯上限
            can_deposit = min(account['wallet'], account['space_to_tier1'])
            if can_deposit >= min_purchase_amount:  # 最小申購金額
                operations.append({
                    'account_id': account['id'],
                    'account_name': account['name'],
                    'action': 'subscribe',
                    'amount': can_deposit,
                    'reason': f"申購 {format_amount(can_deposit)} (錢包可用: {format_amount(account['wallet'])})"
                })
            else:
                print(f"  跳過 {account['name']}: 錢包餘額不足最小申購金額{format_amount(min_purchase_amount)} (當前: {format_amount(account['wallet'])})")
        elif op_choice == '2':  # 取出到剩300
            if account['holding'] > tier1_limit:
                # 計算贖回金額並 round 到 8 位，避免浮點誤差
                redeem_amount = round(account['holding'] - tier1_limit, 8)
                # round 後 > 0 才處理
                if redeem_amount > 0:
                    operations.append({
                        'account_id': account['id'],
                        'account_name': account['name'],
                        'action': 'redeem',
                        'amount': redeem_amount
                    })
        elif op_choice == '3':  # 全部取出
            # round 到 8 位，避免浮點誤差
            redeem_amount = round(account['holding'], 8)
            # round 後 > 0 才處理
            if redeem_amount > 0:
                operations.append({
                    'account_id': account['id'],
                    'account_name': account['name'],
                    'action': 'redeem',
                    'amount': redeem_amount
                })
    
    # 顯示操作計劃
    if not operations:
        print("\n[信息] 沒有需要執行的操作")
        print("當前帳戶狀態已符合選擇的策略目標")
        return [], subscribe_precision
    
    print(f"\n[操作計劃]")
    for op in operations:
        action_text = "申購" if op['action'] == 'subscribe' else "贖回"
        print(f"  {op['account_name']}: {action_text} {op['amount']} {coin}")
    
    # 確認執行
    try:
        confirm = input(f"\n確認執行以上操作? (y/N): ").strip().lower()
        if confirm != 'y':
            print("[取消] 用戶取消操作")
            return None, subscribe_precision
    except KeyboardInterrupt:
        print("[取消] 用戶取消操作")
        return None, subscribe_precision
    
    return operations, subscribe_precision


def step4_execute_operations(coin, selected_product, operations, subscribe_precision=6):
    """步驟4: 執行申購/贖回操作"""
    print(f"\n=== 步驟4: 執行操作 ===")
    
    if not operations:
        print("[信息] 沒有操作需要執行")
        return True
    
    product_id = selected_product.get('productId')
    period_type = selected_product.get('periodType')
    
    success_count = 0
    total_count = len(operations)
    
    print(f"[信息] 開始執行 {total_count} 個操作...")
    
    for i, op in enumerate(operations):
        account_id = op['account_id']
        account_name = op['account_name']
        action = op['action']
        amount = op['amount']
        
        print(f"\n[執行 {i+1}/{total_count}] {account_name} - ", end="")
        
        try:
            if action == 'subscribe':
                # 8 位精度格式化
                formatted_amount = format_api_amount(amount)
                print(f"申購 {formatted_amount} {coin}")
                result = savings_subscribe(product_id, period_type, formatted_amount, account_key=account_id)
            elif action == 'redeem':
                # 8 位精度格式化
                formatted_amount = format_api_amount(amount)
                print(f"贖回 {formatted_amount} {coin}")
                result = savings_redeem(product_id, period_type, formatted_amount, account_key=account_id)
            else:
                print(f"[錯誤] 未知操作類型: {action}")
                continue
            
            # 檢查執行結果
            if result.get('code') == '00000':
                order_id = result.get('data', {}).get('orderId', '')
                print(f"  ✅ 成功 (訂單ID: {order_id})")
                success_count += 1
            else:
                error_msg = result.get('msg', '未知錯誤')
                print(f"  ❌ 失敗: {error_msg}")
                print(f"     詳細: {result}")
            
            # 輕微延遲避免過於頻繁請求
            if i < total_count - 1:  # 最後一次不需要等待
                time.sleep(0.2)
                
        except Exception as e:
            print(f"  ❌ 異常: {e}")
    
    print(f"\n[完成] 操作執行完成")
    print(f"  成功: {success_count}/{total_count}")
    print(f"  失敗: {total_count - success_count}/{total_count}")
    
    
    return True


def step5_final_query(coin, selected_product, original_account_status):
    """步驟5: 再次查詢並顯示最終狀況"""
    print(f"\n=== 步驟5: 最終狀況查詢 ===")
    
    product_id = selected_product.get('productId')
    period_type = selected_product.get('periodType')
    product_name = "活期" if period_type == 'flexible' else f"{selected_product.get('period')}天定期"
    
    print(f"[等待] 等待5秒讓申購操作結算...")
    time.sleep(5)
    
    print(f"[查詢] {product_name}產品最新狀況...")
    
    # 重新查詢所有帳戶狀況
    final_account_status = step2_query_current_assets(coin, selected_product)
    if not final_account_status:
        print("[錯誤] 無法查詢最終狀況")
        return False
    
    # 顯示前後對比
    print(f"\n=== 📊 執行結果對比 ===")
    
    # 解析產品階梯信息
    apy_list = selected_product.get('apyList', [])
    tier1_limit = safe_float(apy_list[0].get('maxStepVal', '0')) if apy_list else 0.0
    
    print(f"產品: {product_name} (第一階梯上限: {tier1_limit} {coin})")
    print(f"{'帳戶':<8} {'執行前持有':<12} {'執行前錢包':<12} {'執行後持有':<12} {'執行後錢包':<12} {'變化':<20}")
    print("-" * 80)
    
    total_before_holding = 0
    total_after_holding = 0
    total_before_wallet = 0
    total_after_wallet = 0
    
    for account_id in final_account_status.keys():
        # 執行前數據
        before_data = original_account_status.get(account_id, {})
        before_holding = get_account_holding(before_data, product_id)
        before_wallet = get_account_wallet(before_data)
        
        # 執行後數據  
        after_data = final_account_status.get(account_id, {})
        after_holding = get_account_holding(after_data, product_id)
        after_wallet = get_account_wallet(after_data)
        
        # 計算變化
        holding_change = after_holding - before_holding
        wallet_change = after_wallet - before_wallet
        
        # 變化描述
        if abs(holding_change) < 1e-8:
            change_desc = "無變化"
        elif holding_change > 0:
            change_desc = f"申購 +{format_amount(holding_change)}"
        else:
            change_desc = f"贖回 {format_amount(holding_change)}"
        
        # 帳戶名稱
        account_type = after_data.get('account_info', {}).get('type', '')
        account_name = "主帳戶" if account_type == 'main' else f"子帳戶{account_id}"
        
        print(f"{account_name:<8} {format_amount(before_holding):<12} {format_amount(before_wallet):<12} {format_amount(after_holding):<12} {format_amount(after_wallet):<12} {change_desc:<20}")
        
        # 累計統計
        total_before_holding += before_holding
        total_after_holding += after_holding  
        total_before_wallet += before_wallet
        total_after_wallet += after_wallet
    
    # 顯示總計
    print("-" * 80)
    total_holding_change = total_after_holding - total_before_holding
    total_wallet_change = total_after_wallet - total_before_wallet
    
    if abs(total_holding_change) < 1e-8:
        total_change_desc = "無變化"
    elif total_holding_change > 0:
        total_change_desc = f"總申購 +{format_amount(total_holding_change)}"
    else:
        total_change_desc = f"總贖回 {format_amount(total_holding_change)}"
    
    print(f"{'總計':<8} {format_amount(total_before_holding):<12} {format_amount(total_before_wallet):<12} {format_amount(total_after_holding):<12} {format_amount(total_after_wallet):<12} {total_change_desc:<20}")
    
    # 階梯分析
    print(f"\n=== 📈 階梯分析 ===")
    tier1_accounts = 0
    tier2_accounts = 0
    
    for account_id in final_account_status.keys():
        after_data = final_account_status.get(account_id, {})
        after_holding = get_account_holding(after_data, product_id)
        account_type = after_data.get('account_info', {}).get('type', '')
        account_name = "主帳戶" if account_type == 'main' else f"子帳戶{account_id}"
        
        if after_holding > tier1_limit and len(apy_list) > 1:
            tier2_accounts += 1
            tier2_apy = apy_list[1].get('currentApy', '0')
            print(f"  {account_name}: {after_holding} {coin} (第二階梯 {tier2_apy}%)")
        elif after_holding > tier1_limit:
            # 只有一個階梯，但超過了上限（理論上不應該發生）
            tier1_accounts += 1
            tier1_apy = apy_list[0].get('currentApy', '0')
            print(f"  {account_name}: {after_holding} {coin} (超過第一階梯上限 {tier1_apy}%)")
        elif after_holding > 0.0:
            tier1_accounts += 1
            tier1_apy = apy_list[0].get('currentApy', '0')
            space_left = tier1_limit - after_holding
            print(f"  {account_name}: {after_holding} {coin} (第一階梯 {tier1_apy}%, 還可存{space_left})")
        else:
            print(f"  {account_name}: 0.00 {coin} (未投資)")
    
    if len(apy_list) > 1:
        print(f"\n第一階梯帳戶數: {tier1_accounts}, 第二階梯帳戶數: {tier2_accounts}")
    else:
        print(f"\n投資帳戶數: {tier1_accounts} (此產品只有單一利率階梯)")
    
    return True


def get_account_holding(account_data, product_id):
    """從帳戶數據中提取指定產品的持有量"""
    savings_result = account_data.get('savings_result', {})
    if savings_result.get('code') == '00000':
        result_list = savings_result.get('data', {}).get('resultList', [])
        for item in result_list:
            if item.get('productId') == product_id:
                return safe_float(item.get('holdAmount', '0'))
    return 0.0


def get_account_wallet(account_data):
    """從帳戶數據中提取錢包餘額"""
    wallet_result = account_data.get('wallet_result', {})
    if wallet_result.get('code') == '00000' and wallet_result.get('data'):
        wallet_data = wallet_result.get('data', [])
        if wallet_data:
            return safe_float(wallet_data[0].get('available', '0'))
    return 0.0


def transfer_management_workflow():
    """主子帳戶轉帳管理主流程"""
    print("\n=== 主子帳戶轉帳管理 ===")
    
    # 步驟0: 確保主帳戶UID已記錄
    print("\n[步驟0] 檢查主帳戶UID配置...")
    if not ensure_main_account_uid():
        print("[錯誤] 主帳戶UID配置失敗，無法進行轉帳操作")
        return False
    
    # 步驟1: 讓用戶輸入選擇幣種
    coin = input("\n請輸入要轉帳的幣種 (例如: USDT, BTC, ETH): ").strip().upper()
    if not coin:
        print("[錯誤] 幣種不能為空")
        return False
        
    print(f"[信息] 選擇的幣種: {coin}")
    print("=" * 50)
    
    # 步驟2: 查詢每個帳戶餘額並顯示
    account_balances = transfer_step1_query_balances(coin)
    if not account_balances:
        return False
    
    # 步驟3: 用戶選擇轉帳策略
    operations = transfer_step2_user_selection(coin, account_balances)
    if operations is None:  # 用戶取消或錯誤
        return False
    
    # 步驟4: 執行轉帳操作
    if not transfer_step3_execute_operations(coin, operations):
        return False
    
    # 步驟5: 再次查詢並顯示最終狀況
    if not transfer_step4_final_query(coin, account_balances):
        return False
    
    print("\n" + "=" * 50)
    print("[完成] 轉帳管理流程執行完成")
    return True


def transfer_step1_query_balances(coin):
    """步驟1: 查詢所有帳戶的指定幣種餘額 (並行查詢)"""
    print(f"\n=== 步驟1: 查詢所有帳戶 {coin} 餘額 ===")
    
    # 使用 AccountManager 並行查詢
    manager = AccountManager()
    accounts = manager.get_valid_accounts()
    
    if not accounts:
        print("[錯誤] 沒有找到有效的帳戶配置")
        return False
    
    print(f"[信息] 正在並行查詢 {len(accounts)} 個帳戶的 {coin} 餘額...")
    
    # 並行查詢所有帳戶的現貨資產
    account_balances = manager.query_all_spot_assets(coin)
    
    if not account_balances:
        print("[錯誤] 查詢失敗")
        return False
    
    # 顯示總覽表格 (使用 BalanceParser)
    parser = BalanceParser()
    
    print(f"\n=== {coin} 餘額總覽 ===")
    print(f"{'帳戶':<12} {'類型':<6} {'可用餘額':<15} {'凍結餘額':<15} {'總餘額':<15}")
    print("-" * 70)
    
    total_available = 0.0
    total_frozen = 0.0
    
    for account_id, balance_info in account_balances.items():
        account_type = balance_info['account_info'].get('type')
        account_name = parser.format_account_name(account_type, account_id)
        
        # 使用 BalanceParser 解析餘額
        wallet_result = balance_info.get('wallet_result', {})
        available, frozen = parser.parse_spot_balance(wallet_result)
        
        total_balance = available + frozen
        print(f"{account_name:<12} {account_type:<6} {format_amount(available):<15} {format_amount(frozen):<15} {format_amount(total_balance):<15}")
        
        total_available += available
        total_frozen += frozen
    
    print("-" * 70)
    total_all = total_available + total_frozen
    print(f"{'總計':<12} {'--':<6} {format_amount(total_available):<15} {format_amount(total_frozen):<15} {format_amount(total_all):<15}")
    
    return account_balances


def transfer_step2_user_selection(coin, account_balances):
    """步驟2: 用戶選擇轉帳策略"""
    print(f"\n=== 步驟2: 選擇轉帳策略 ===")
    
    # 分析帳戶狀況
    main_balance = 0.0
    sub_accounts = []
    
    for account_id, balance_info in account_balances.items():
        account_type = balance_info['account_info'].get('type')
        
        wallet_result = balance_info.get('wallet_result', {})
        available = 0
        if wallet_result.get('code') == '00000' and wallet_result.get('data'):
            wallet_data = wallet_result.get('data', [])
            if wallet_data:
                available = safe_float(wallet_data[0].get('available', '0'))
        
        if account_type == 'main':
            main_balance = available
        else:
            sub_accounts.append({
                'id': account_id,
                'name': f'子帳戶{account_id}',
                'uuid': balance_info['account_info'].get('uuid'),
                'balance': available
            })
    
    # 用戶選擇轉帳方向
    print(f"\n[轉帳方向選擇]")
    print("1. 主帳戶轉出到子帳戶")
    print("2. 子帳戶轉回主帳戶")
    
    try:
        direction_choice = input("請選擇轉帳方向 (1-2): ").strip()
        if direction_choice not in ['1', '2']:
            print("[錯誤] 無效選擇")
            return None
    except KeyboardInterrupt:
        print("[取消] 用戶取消操作")
        return None
    
    operations = []
    
    if direction_choice == '1':  # 主轉子
        print(f"\n[主帳戶轉出] 主帳戶可用餘額: {format_amount(main_balance)} {coin}")
        
        if main_balance <= 0.0:
            print("[錯誤] 主帳戶餘額不足")
            return None
        
        # 輸入每個帳號的轉帳金額
        try:
            amount_input = input(f"請輸入每個帳號的轉帳金額: ").strip()
            transfer_amount_per_account = safe_float(amount_input)
            if transfer_amount_per_account <= 0.0:
                print("[錯誤] 轉帳金額必須大於0")
                return None
        except (ValueError, KeyboardInterrupt):
            print("[錯誤] 金額格式錯誤或用戶取消")
            return None
        
        # 選擇目標子帳戶
        print(f"\n[目標選擇]")
        print("0. 所有子帳戶")
        for i, sub in enumerate(sub_accounts):
            print(f"{i+1}. {sub['name']} (當前餘額: {format_amount(sub['balance'])})")
        print(f"多選範例: 輸入 1,2,3 選擇多個帳戶")
        
        try:
            target_choice = input(f"請選擇目標 (0-{len(sub_accounts)} 或多選如 1,2,3): ").strip()
            
            # 解析選擇
            selected_subs = []
            
            if target_choice == '0':
                # 所有子帳戶
                selected_subs = sub_accounts
                print("[選擇] 所有子帳戶")
            elif ',' in target_choice:
                # 多選格式，如1,2,3
                try:
                    target_numbers = [int(x.strip()) for x in target_choice.split(',')]
                    for num in target_numbers:
                        if 1 <= num <= len(sub_accounts):
                            selected_subs.append(sub_accounts[num - 1])
                        else:
                            print(f"[錯誤] 無效選擇: {num}")
                            return None
                    sub_names = [sub['name'] for sub in selected_subs]
                    print(f"[選擇] {', '.join(sub_names)}")
                except ValueError:
                    print("[錯誤] 多選格式錯誤，請使用如 1,2,3 的格式")
                    return None
            else:
                # 單選
                target_num = int(target_choice)
                if 1 <= target_num <= len(sub_accounts):
                    selected_subs = [sub_accounts[target_num - 1]]
                    print(f"[選擇] {selected_subs[0]['name']}")
                else:
                    print("[錯誤] 無效選擇")
                    return None
            
            # 檢查總金額是否超出主帳戶餘額
            total_transfer_amount = transfer_amount_per_account * len(selected_subs)
            if total_transfer_amount > main_balance:
                print(f"[錯誤] 總轉帳金額 {total_transfer_amount} 超過主帳戶餘額 {main_balance}")
                
                # 計算在當前金額下最多可以轉幾個帳戶
                max_accounts = int(main_balance / transfer_amount_per_account)
                if max_accounts > 0:
                    remaining_balance = main_balance - (max_accounts * transfer_amount_per_account)
                    print(f"[建議] 以每個 {transfer_amount_per_account} {coin} 計算，最多可轉 {max_accounts} 個帳戶")
                    print(f"        這樣會用掉 {max_accounts * transfer_amount_per_account} {coin}，剩餘 {remaining_balance} {coin}")
                    
                    # 詢問用戶是否要選前N個帳戶
                    try:
                        auto_select = input(f"是否轉帳到前 {max_accounts} 個選中的帳戶? (y/N): ").strip().lower()
                        if auto_select == 'y':
                            # 自動選擇前N個帳戶
                            selected_subs = selected_subs[:max_accounts]
                            selected_names = [sub['name'] for sub in selected_subs]
                            print(f"[自動調整] 將轉帳到: {', '.join(selected_names)}")
                            print(f"[新計劃] 總轉帳金額: {max_accounts * transfer_amount_per_account} {coin}")
                        else:
                            print("[取消] 請重新輸入轉帳金額或選擇帳戶")
                            return None
                    except KeyboardInterrupt:
                        print("[取消] 用戶取消操作")
                        return None
                else:
                    print(f"[錯誤] 主帳戶餘額不足以轉帳 {transfer_amount_per_account} {coin} 到任何帳戶")
                    return None
            
            # 創建轉帳操作
            for sub in selected_subs:
                operations.append({
                    'type': 'main_to_sub',
                    'from_account': 'main',
                    'to_account': sub['id'],
                    'to_uuid': sub['uuid'],
                    'amount': transfer_amount_per_account,
                    'description': f"主帳戶 → {sub['name']}: {transfer_amount_per_account} {coin}"
                })
                
        except (ValueError, KeyboardInterrupt):
            print("[錯誤] 選擇無效或用戶取消")
            return None
            
    else:  # 子轉主
        print(f"\n[子帳戶轉回]")
        
        # 顯示有餘額的子帳戶
        subs_with_balance = [sub for sub in sub_accounts if sub['balance'] > 0.0]
        if not subs_with_balance:
            print("[錯誤] 沒有子帳戶有餘額")
            return None
        
        # 選擇轉回方式
        print("[轉回方式選擇]")
        print("1. 全部轉回（每個子帳戶的全部餘額）")
        print("2. 指定金額轉回（每個選中的子帳戶轉回相同金額）")
        
        try:
            transfer_mode = input("請選擇轉回方式 (1-2): ").strip()
            if transfer_mode not in ['1', '2']:
                print("[錯誤] 無效選擇")
                return None
        except KeyboardInterrupt:
            print("[取消] 用戶取消操作")
            return None
        
        # 根據轉回方式處理
        if transfer_mode == '1':
            # 全部轉回模式 - 選擇帳戶
            print(f"\n[帳戶選擇]")
            print("0. 所有有餘額的子帳戶")
            for i, sub in enumerate(subs_with_balance):
                print(f"{i+1}. {sub['name']} (餘額: {format_amount(sub['balance'])})")
            print(f"多選範例: 輸入 1,2,3 選擇多個帳戶")
            
            try:
                source_choice = input(f"請選擇來源 (0-{len(subs_with_balance)} 或多選如 1,2,3): ").strip()
                
                # 解析選擇
                selected_subs = []
                
                if source_choice == '0':
                    # 所有有餘額的子帳戶
                    selected_subs = subs_with_balance
                    print("[選擇] 所有有餘額的子帳戶")
                elif ',' in source_choice:
                    # 多選格式
                    try:
                        source_numbers = [int(x.strip()) for x in source_choice.split(',')]
                        for num in source_numbers:
                            if 1 <= num <= len(subs_with_balance):
                                selected_subs.append(subs_with_balance[num - 1])
                            else:
                                print(f"[錯誤] 無效選擇: {num}")
                                return None
                        sub_names = [sub['name'] for sub in selected_subs]
                        print(f"[選擇] {', '.join(sub_names)}")
                    except ValueError:
                        print("[錯誤] 多選格式錯誤，請使用如 1,2,3 的格式")
                        return None
                else:
                    # 單選
                    source_num = int(source_choice)
                    if 1 <= source_num <= len(subs_with_balance):
                        selected_subs = [subs_with_balance[source_num - 1]]
                        print(f"[選擇] {selected_subs[0]['name']}")
                    else:
                        print("[錯誤] 無效選擇")
                        return None
                
                # 全部轉回
                for sub in selected_subs:
                    operations.append({
                        'type': 'sub_to_main',
                        'from_account': sub['id'],
                        'from_uuid': sub['uuid'],
                        'to_account': 'main',
                        'amount': sub['balance'],
                        'description': f"{sub['name']} → 主帳戶: {format_amount(sub['balance'])} {coin} (全部餘額)"
                    })
                    
            except (ValueError, KeyboardInterrupt):
                print("[錯誤] 選擇無效或用戶取消")
                return None
        
        else:  # transfer_mode == '2'
            # 指定金額轉回模式
            try:
                amount_input = input(f"請輸入每個帳號的轉回金額: ").strip()
                transfer_amount_per_account = safe_float(amount_input)
                if transfer_amount_per_account <= 0.0:
                    print("[錯誤] 轉回金額必須大於0")
                    return None
            except (ValueError, KeyboardInterrupt):
                print("[錯誤] 金額格式錯誤或用戶取消")
                return None
            
            # 篩選出餘額足夠的子帳戶
            eligible_subs = [sub for sub in subs_with_balance if sub['balance'] >= transfer_amount_per_account]
            if not eligible_subs:
                print(f"[錯誤] 沒有子帳戶的餘額 >= {transfer_amount_per_account}")
                return None
            
            print(f"\n[帳戶選擇] (餘額 >= {transfer_amount_per_account})")
            print("0. 所有符合條件的子帳戶")
            for i, sub in enumerate(eligible_subs):
                print(f"{i+1}. {sub['name']} (餘額: {format_amount(sub['balance'])})")
            print(f"多選範例: 輸入 1,2,3 選擇多個帳戶")
            
            try:
                source_choice = input(f"請選擇來源 (0-{len(eligible_subs)} 或多選如 1,2,3): ").strip()
                
                # 解析選擇
                selected_subs = []
                
                if source_choice == '0':
                    # 所有符合條件的子帳戶
                    selected_subs = eligible_subs
                    print("[選擇] 所有符合條件的子帳戶")
                elif ',' in source_choice:
                    # 多選格式
                    try:
                        source_numbers = [int(x.strip()) for x in source_choice.split(',')]
                        for num in source_numbers:
                            if 1 <= num <= len(eligible_subs):
                                selected_subs.append(eligible_subs[num - 1])
                            else:
                                print(f"[錯誤] 無效選擇: {num}")
                                return None
                        sub_names = [sub['name'] for sub in selected_subs]
                        print(f"[選擇] {', '.join(sub_names)}")
                    except ValueError:
                        print("[錯誤] 多選格式錯誤，請使用如 1,2,3 的格式")
                        return None
                else:
                    # 單選
                    source_num = int(source_choice)
                    if 1 <= source_num <= len(eligible_subs):
                        selected_subs = [eligible_subs[source_num - 1]]
                        print(f"[選擇] {selected_subs[0]['name']}")
                    else:
                        print("[錯誤] 無效選擇")
                        return None
                
                # 指定金額轉回
                for sub in selected_subs:
                    operations.append({
                        'type': 'sub_to_main',
                        'from_account': sub['id'],
                        'from_uuid': sub['uuid'],
                        'to_account': 'main',
                        'amount': transfer_amount_per_account,
                        'description': f"{sub['name']} → 主帳戶: {transfer_amount_per_account} {coin}"
                    })
                    
            except (ValueError, KeyboardInterrupt):
                print("[錯誤] 選擇無效或用戶取消")
                return None
    
    # 顯示操作計劃
    if not operations:
        print("\n[信息] 沒有需要執行的轉帳操作")
        return []
    
    print(f"\n[轉帳計劃]")
    total_amount = 0
    for op in operations:
        print(f"  {op['description']}")
        total_amount += op['amount']
    
    print(f"\n[總計] 將轉帳 {format_amount(total_amount)} {coin}")
    
    # 確認執行
    try:
        confirm = input(f"\n確認執行以上轉帳操作? (y/N): ").strip().lower()
        if confirm != 'y':
            print("[取消] 用戶取消操作")
            return None
    except KeyboardInterrupt:
        print("[取消] 用戶取消操作")
        return None
    
    return operations


def transfer_step3_execute_operations(coin, operations):
    """步驟3: 執行轉帳操作 (受控並行)"""
    print(f"\n=== 步驟3: 執行轉帳操作 ===")
    
    if not operations:
        print("[信息] 沒有操作需要執行")
        return True
    
    import math
    
    success_count = 0
    total_count = len(operations)
    optimal_precision = None
    
    print(f"[信息] 開始執行 {total_count} 個轉帳操作...")
    
    # ===== Step 1: 第一筆精度測試 (必須串行) =====
    first_op = operations[0]
    first_transfer_type = first_op['type']
    first_amount = first_op['amount']
    first_description = first_op['description']
    
    formatted_first = format_amount(first_amount)
    formatted_desc = first_description.replace(f'{first_amount} {coin}', f'{formatted_first} {coin}')
    print(f"\n[執行 1/{total_count}] {formatted_desc}")
    print(f"[精度測試] 正在測試最佳轉帳精度...")
    
    result = find_optimal_precision_and_execute(first_transfer_type, first_op, coin, first_amount)
    
    if result is None:
        print(f"[錯誤] 無法找到可用的轉帳精度，終止操作")
        return False
    
    optimal_precision, first_result = result
    print(f"[找到] 最佳精度: {optimal_precision} 位小數")
    
    if first_result.get('code') == '00000':
        transfer_id = first_result.get('data', {}).get('transferId', '')
        print(f"  [OK] 成功 (轉帳ID: {transfer_id})")
        success_count += 1
    else:
        error_msg = first_result.get('msg', '未知錯誤')
        print(f"  [ERROR] 失敗: {error_msg}")
    
    # 如果只有一筆操作，直接結束
    if total_count == 1:
        print(f"\n[完成] 轉帳操作執行完成")
        print(f"  成功: {success_count}/{total_count}")
        return True
    
    # ===== Step 2: 準備剩餘操作的並行任務 =====
    remaining_ops = operations[1:]
    main_account_uid = get_main_account_uid()
    
    def execute_single_transfer(op, precision, coin, main_uid):
        """執行單筆轉帳的內部函數"""
        transfer_type = op['type']
        amount = op['amount']
        
        # 精度調整
        precision_factor = 10 ** precision
        adjusted_amount = math.floor(amount * precision_factor) / precision_factor
        api_amount = format_api_amount(adjusted_amount)
        
        if transfer_type == 'main_to_sub':
            return transfer_to_subaccount(
                coin=coin,
                amount=api_amount,
                sub_account_uid=op['to_uuid'],
                account_key='main'
            )
        elif transfer_type == 'sub_to_main':
            if main_uid:
                return transfer_to_main_account(
                    coin=coin,
                    amount=api_amount,
                    sub_account_uid=op['from_uuid'],
                    main_account_uid=main_uid,
                    account_key='main'
                )
            else:
                return {'code': 'ERROR', 'msg': '配置中找不到主帳戶UID'}
        else:
            return {'code': 'ERROR', 'msg': f'未知轉帳類型: {transfer_type}'}
    
    # 構建任務列表
    tasks = [
        (execute_single_transfer, (op, optimal_precision, coin, main_account_uid))
        for op in remaining_ops
    ]
    
    # ===== Step 3: 分批並行執行 =====
    print(f"\n[並行執行] 開始分批執行剩餘 {len(remaining_ops)} 筆轉帳 (每批 8 筆)...")
    
    executor = ParallelExecutor()
    batch_results = executor.execute_in_batches(tasks, batch_size=8, delay_between_batches=1.0)
    
    # ===== Step 4: 顯示結果 =====
    for idx, result in batch_results:
        op = remaining_ops[idx]
        op_num = idx + 2  # 第一筆已執行，從第 2 筆開始
        
        formatted_amount = format_amount(op['amount'])
        desc = op['description'].replace(f"{op['amount']} {coin}", f"{formatted_amount} {coin}")
        
        if result.get('code') == '00000':
            transfer_id = result.get('data', {}).get('transferId', '')
            print(f"  [{op_num}/{total_count}] ✓ {desc} (ID: {transfer_id})")
            success_count += 1
        else:
            error_msg = result.get('msg', result.get('error', '未知錯誤'))
            print(f"  [{op_num}/{total_count}] ✗ {desc} - {error_msg}")
    
    print(f"\n[完成] 轉帳操作執行完成")
    print(f"  成功: {success_count}/{total_count}")
    print(f"  失敗: {total_count - success_count}/{total_count}")
    
    return True


def transfer_step4_final_query(coin, original_balances):
    """步驟4: 再次查詢並顯示最終狀況"""
    print(f"\n=== 步驟4: 最終餘額查詢 ===")
    
    print(f"[等待] 等待5秒讓轉帳操作結算...")
    time.sleep(5)
    
    print(f"[查詢] {coin} 最新餘額...")
    
    # 重新查詢所有帳戶餘額
    final_balances = transfer_step1_query_balances(coin)
    if not final_balances:
        print("[錯誤] 無法查詢最終餘額")
        return False
    
    # 顯示前後對比
    print(f"\n=== 轉帳結果對比 ===")
    print(f"{'帳戶':<12} {'轉帳前':<15} {'轉帳後':<15} {'變化':<20}")
    print("-" * 65)
    
    total_before = 0.0
    total_after = 0.0
    
    for account_id in final_balances.keys():
        # 轉帳前餘額
        before_data = original_balances.get(account_id, {})
        before_balance = get_account_spot_balance(before_data, coin)
        
        # 轉帳後餘額
        after_data = final_balances.get(account_id, {})
        after_balance = get_account_spot_balance(after_data, coin)
        
        # 計算變化
        balance_change = after_balance - before_balance
        
        # 變化描述
        if abs(balance_change) < 1e-8:  # 使用更小的閾值來識別微小變化
            change_desc = "無變化"
        elif balance_change > 0.0:
            change_desc = f"轉入 +{format_amount(balance_change)}"
        else:
            change_desc = f"轉出 {format_amount(balance_change)}"
        
        # 帳戶名稱
        account_type = after_data.get('account_info', {}).get('type', '')
        account_name = "主帳戶" if account_type == 'main' else f"子帳戶{account_id}"
        
        print(f"{account_name:<12} {format_amount(before_balance):<15} {format_amount(after_balance):<15} {change_desc:<20}")
        
        # 累計統計
        total_before += before_balance
        total_after += after_balance
    
    # 顯示總計
    print("-" * 65)
    total_change = total_after - total_before
    if abs(total_change) < 1e-8:
        total_change_desc = "無變化"
    else:
        total_change_desc = f"淨變化 {format_amount(total_change)}"
    
    print(f"{'總計':<12} {format_amount(total_before):<15} {format_amount(total_after):<15} {total_change_desc:<20}")
    
    return True


def find_optimal_precision_and_execute(transfer_type, op, coin, amount):
    """統一使用 8 位精度執行轉帳
    
    Returns:
        tuple: (8, 轉帳結果) 或 None (如果失敗)
    """
    # 統一使用 8 位精度
    test_amount = round(amount, 8)
    
    # 如果調整後金額為0，跳過
    if test_amount <= 0:
        return None
        
    print(f"  測試轉帳金額: {format_amount(test_amount)}")
    
    # 執行測試轉帳
    try:
        if transfer_type == 'main_to_sub':
            # 格式化金額為API字符串格式
            api_test_amount = format_api_amount(test_amount)
            result = transfer_to_subaccount(
                coin=coin,
                amount=api_test_amount,
                sub_account_uid=op['to_uuid'],
                account_key='main'
            )
        elif transfer_type == 'sub_to_main':
            main_account_uid = get_main_account_uid()
            if main_account_uid:
                # 格式化金額為API字符串格式
                api_test_amount = format_api_amount(test_amount)
                result = transfer_to_main_account(
                    coin=coin,
                    amount=api_test_amount,
                    sub_account_uid=op['from_uuid'],
                    main_account_uid=main_account_uid,
                    account_key='main'
                )
            else:
                return None
        else:
            return None
                
        # 檢查結果
        if result.get('code') == '00000':
            print(f"  ✓ 轉帳成功")
            return (8, result)  # 返回固定 8 位精度和轉帳結果
        else:
            print(f"  ✗ 轉帳失敗: {result.get('msg', '未知錯誤')}")
            return None
                
    except Exception as e:
        print(f"  ✗ 轉帳異常: {e}")
        return None
    
    return None


def get_account_spot_balance(account_data, coin=None):
    """從帳戶數據中提取現貨可用餘額"""
    wallet_result = account_data.get('wallet_result', {})
    if wallet_result.get('code') == '00000' and wallet_result.get('data'):
        wallet_data = wallet_result.get('data', [])
        if wallet_data:
            available = safe_float(wallet_data[0].get('available', '0'))
            return available
    return 0.0


def ensure_main_account_uid():
    """確保主帳戶UID已記錄在配置中"""
    config = load_config()
    if not config:
        print("[錯誤] 無法載入配置文件")
        return False
    
    # 檢查配置中是否已有主帳戶UID
    main_config = config.get('accounts', {}).get('main', {})
    main_uid = main_config.get('uuid')
    
    if main_uid:
        print(f"[信息] 主帳戶UID已存在: {main_uid}")
        return True
    
    # 如果沒有，調用API獲取並保存
    print("[信息] 配置中沒有主帳戶UID，正在查詢並記錄...")
    
    try:
        account_info_result = get_account_info('main')
        print(f"[DEBUG] 帳戶信息API返回: {account_info_result}")
        
        if account_info_result.get('code') == '00000':
            data = account_info_result.get('data', {})
            main_uid = data.get('userId')
            
            if main_uid:
                # 更新配置文件
                config['accounts']['main']['uuid'] = main_uid
                save_config(config)
                print(f"[成功] 已獲取並保存主帳戶UID: {main_uid}")
                return True
            else:
                print("[錯誤] API返回中沒有找到userId字段")
                print(f"[DEBUG] 完整data內容: {data}")
        else:
            print(f"[錯誤] 獲取帳戶信息失敗: {account_info_result.get('msg', '未知錯誤')}")
    except Exception as e:
        print(f"[錯誤] 獲取帳戶信息異常: {e}")
    
    print("[解決方案] 請手動在配置文件的main帳戶中添加'uuid'字段")
    return False


def get_main_account_uid():
    """從配置中獲取主帳戶UID"""
    config = load_config()
    if not config:
        return None
    
    main_config = config.get('accounts', {}).get('main', {})
    return main_config.get('uuid')


# ===== 快速操作功能 =====

QUICK_OP_COINS = ['USDT', 'BTC', 'ETH', 'USDC']


def quick_operations_menu():
    """快速操作選單"""
    print("\n=== 快速操作 ===")
    print("1. 一鍵分發到所有帳戶並填滿第一階梯 (USDT/BTC/ETH/USDC)")
    print("2. 取出到剩第一階梯，全部轉回主帳號")
    print("3. 全部取出，全部轉回主帳號")
    print("0. 返回主選單")
    print("================")

    try:
        choice = input("請選擇: ").strip()
        if choice == '1':
            return quick_distribute_and_fill_tier1()
        elif choice == '2':
            return quick_redeem_and_collect(keep_tier1=True)
        elif choice == '3':
            return quick_redeem_and_collect(keep_tier1=False)
        elif choice == '0':
            return True
        else:
            print("[錯誤] 無效選擇")
            return True
    except KeyboardInterrupt:
        print("\n[取消]")
        return True


def quick_distribute_and_fill_tier1():
    """一鍵分發到所有帳戶並填滿第一階梯"""
    print("\n=== 一鍵分發並填滿第一階梯 ===")
    print(f"[信息] 將處理幣種: {', '.join(QUICK_OP_COINS)}")

    # 確保主帳戶UID已記錄
    if not ensure_main_account_uid():
        print("[錯誤] 主帳戶UID配置失敗")
        return False

    # 逐個幣種處理
    for coin in QUICK_OP_COINS:
        print(f"\n{'='*50}")
        print(f"[處理] {coin}")
        print('='*50)

        success = process_coin_distribute_and_fill(coin)
        if not success:
            print(f"[警告] {coin} 處理過程中有錯誤，繼續下一個幣種")

    print(f"\n{'='*50}")
    print("[完成] 所有幣種處理完畢")
    return True


def quick_redeem_and_collect(keep_tier1=False):
    """取出理財並轉回主帳號

    Args:
        keep_tier1: True=保留第一階梯, False=全部取出
    """
    mode_text = "取出到剩第一階梯" if keep_tier1 else "全部取出"
    print(f"\n=== {mode_text}，轉回主帳號 ===")
    print(f"[信息] 將處理幣種: {', '.join(QUICK_OP_COINS)}")

    # 確保主帳戶UID已記錄
    if not ensure_main_account_uid():
        print("[錯誤] 主帳戶UID配置失敗")
        return False

    # 逐個幣種處理
    for coin in QUICK_OP_COINS:
        print(f"\n{'='*50}")
        print(f"[處理] {coin}")
        print('='*50)

        success = process_coin_redeem_and_collect(coin, keep_tier1)
        if not success:
            print(f"[警告] {coin} 處理過程中有錯誤，繼續下一個幣種")

    print(f"\n{'='*50}")
    print("[完成] 所有幣種處理完畢")
    return True


def process_coin_redeem_and_collect(coin, keep_tier1=False):
    """處理單一幣種：贖回理財並轉回主帳號"""

    mode_text = "保留第一階梯" if keep_tier1 else "全部取出"

    # Step 1: 查詢該幣種的活期產品
    print(f"\n[Step 1] 查詢 {coin} 活期理財產品...")
    product_result = get_savings_products(coin=coin, filter_type='available', account_key='main')

    if product_result.get('code') != '00000':
        print(f"[錯誤] 查詢產品失敗: {product_result}")
        return False

    products = product_result.get('data', [])

    # 找活期產品
    flexible_product = None
    for p in products:
        if p.get('periodType') == 'flexible':
            flexible_product = p
            break

    if not flexible_product:
        print(f"[跳過] {coin} 沒有活期理財產品")
        return True

    product_id = flexible_product.get('productId')
    apy_list = flexible_product.get('apyList', [])
    tier1_limit = safe_float(apy_list[0].get('maxStepVal', '0')) if apy_list else 0.0

    print(f"[產品] {coin} 活期 (ID: {product_id})")
    print(f"[第一階梯上限] {format_amount(tier1_limit)} {coin}")

    # Step 2: 查詢所有帳號的理財持有
    print(f"\n[Step 2] 查詢所有帳號 {coin} 理財持有...")
    manager = AccountManager()
    accounts = manager.get_valid_accounts()

    if not accounts:
        print("[錯誤] 沒有有效帳戶")
        return False

    account_status = manager.query_all_savings_assets(coin, product_id, 'flexible')

    # 整理帳戶資料
    all_accounts = []
    for account_id, status in account_status.items():
        account_type = status['account_info'].get('type')

        # 理財持有量
        holding = 0.0
        savings_result = status.get('savings_result', {})
        if savings_result.get('code') == '00000':
            result_list = savings_result.get('data', {}).get('resultList', [])
            for item in result_list:
                if item.get('productId') == product_id:
                    holding = safe_float(item.get('holdAmount', '0'))
                    break

        all_accounts.append({
            'id': account_id,
            'type': account_type,
            'uuid': status['account_info'].get('uuid'),
            'holding': holding
        })

    # 顯示持有狀況
    print(f"\n[理財持有狀況] {coin}")
    print(f"{'帳戶':<10} {'持有量':<15} {'將贖回':<15}")
    print("-" * 45)

    total_holding = 0.0
    redeem_tasks = []

    for acc in all_accounts:
        name = '主帳戶' if acc['type'] == 'main' else f"子帳戶{acc['id']}"

        if keep_tier1:
            redeem_amount = max(0.0, acc['holding'] - tier1_limit)
        else:
            redeem_amount = acc['holding']

        redeem_amount = round(redeem_amount, 8)  # 精度處理

        print(f"{name:<10} {format_amount(acc['holding']):<15} {format_amount(redeem_amount):<15}")
        total_holding += acc['holding']

        if redeem_amount > 0:
            redeem_tasks.append({
                'account_id': acc['id'],
                'account_name': name,
                'account_type': acc['type'],
                'uuid': acc['uuid'],
                'amount': redeem_amount
            })

    print("-" * 45)
    print(f"{'總計':<10} {format_amount(total_holding):<15}")

    # Step 3: 執行贖回（如果有需要）
    if redeem_tasks:
        print(f"\n[Step 3] 贖回理財 ({len(redeem_tasks)} 個帳號)...")

        def do_redeem(task):
            return savings_redeem(
                product_id, 'flexible',
                format_api_amount(task['amount']),
                account_key=task['account_id']
            )

        # 贖回也可能有限速，一直重試直到全部成功
        pending_tasks = redeem_tasks.copy()
        successful_redeems = []
        max_retries = 10
        retry_delay = 10

        for attempt in range(max_retries + 1):
            if not pending_tasks:
                break

            if attempt > 0:
                print(f"\n[重試 {attempt}] 等待 {retry_delay} 秒後重試 {len(pending_tasks)} 個失敗任務...")
                time.sleep(retry_delay)

            executor = ParallelExecutor()
            tasks_for_executor = [(do_redeem, (t,)) for t in pending_tasks]
            results = executor.execute_in_batches(tasks_for_executor, batch_size=4, delay_between_batches=1.5)

            failed_tasks = []
            for idx, result in results:
                task = pending_tasks[idx]
                if result.get('code') == '00000':
                    print(f"  ✓ {task['account_name']}: {format_amount(task['amount'])} {coin}")
                    successful_redeems.append(task)
                else:
                    error_msg = result.get('msg', '未知錯誤')
                    if 'Frequent' in error_msg and attempt < max_retries:
                        failed_tasks.append(task)
                    else:
                        print(f"  ✗ {task['account_name']}: {error_msg}")

            pending_tasks = failed_tasks

        print(f"[贖回完成] 成功 {len(successful_redeems)}/{len(redeem_tasks)}")

        # 等待贖回結算
        if successful_redeems:
            print("\n[等待] 贖回結算中 (5秒)...")
            time.sleep(5)
    else:
        print(f"\n[Step 3] 沒有需要贖回的 {coin} 理財")

    # Step 5: 查所有子帳號錢包餘額並轉回主帳號
    print(f"\n[Step 4] 查詢所有子帳號錢包餘額並轉回主帳號...")

    # 查詢所有子帳號（不只是贖回成功的）
    sub_accounts_to_transfer = []
    main_uid = get_main_account_uid()

    for acc in all_accounts:
        if acc['type'] == 'sub':
            # 查詢子帳號錢包餘額
            wallet_result = get_spot_assets(coin, acc['id'])
            wallet_balance = 0.0
            if wallet_result.get('code') == '00000' and wallet_result.get('data'):
                wallet_data = wallet_result.get('data', [])
                if wallet_data:
                    wallet_balance = safe_float(wallet_data[0].get('available', '0'))

            if wallet_balance > 0:
                acc_name = f"子帳戶{acc['id']}"
                sub_accounts_to_transfer.append({
                    'account_id': acc['id'],
                    'account_name': acc_name,
                    'uuid': acc['uuid'],
                    'balance': wallet_balance
                })

    if not sub_accounts_to_transfer:
        print("[完成] 沒有子帳號餘額需要轉回")
        return True

    print(f"\n[轉帳] 轉回主帳號 ({len(sub_accounts_to_transfer)} 個子帳號)...")

    def do_transfer_back(task):
        return transfer_to_main_account(
            coin=coin,
            amount=format_api_amount(task['balance']),
            sub_account_uid=task['uuid'],
            main_account_uid=main_uid,
            account_key='main'
        )

    executor = ParallelExecutor()
    tasks_for_executor = [(do_transfer_back, (t,)) for t in sub_accounts_to_transfer]
    results = executor.execute_in_batches(tasks_for_executor, batch_size=8, delay_between_batches=1.0)

    success_count = 0
    for idx, result in results:
        task = sub_accounts_to_transfer[idx]
        if result.get('code') == '00000':
            print(f"  ✓ {task['account_name']}: {format_amount(task['balance'])} {coin}")
            success_count += 1
        else:
            print(f"  ✗ {task['account_name']}: {result.get('msg', '未知錯誤')}")

    print(f"[轉帳完成] 成功 {success_count}/{len(sub_accounts_to_transfer)}")

    print(f"\n[完成] {coin} 處理完畢")
    return True


def process_coin_distribute_and_fill(coin):
    """處理單一幣種：主帳號分發到所有子帳號，然後全部申購到第一階梯上限"""

    # Step 1: 查詢該幣種的活期產品
    print(f"\n[Step 1] 查詢 {coin} 活期理財產品...")
    product_result = get_savings_products(coin=coin, filter_type='available', account_key='main')

    if product_result.get('code') != '00000':
        print(f"[錯誤] 查詢產品失敗: {product_result}")
        return False

    products = product_result.get('data', [])

    # 找活期產品 (periodType = 'flexible')
    flexible_product = None
    for p in products:
        if p.get('periodType') == 'flexible':
            flexible_product = p
            break

    if not flexible_product:
        print(f"[跳過] {coin} 沒有活期理財產品")
        return True

    product_id = flexible_product.get('productId')
    apy_list = flexible_product.get('apyList', [])

    if not apy_list:
        print(f"[錯誤] {coin} 產品沒有階梯信息")
        return False

    # 第一階梯上限
    tier1_limit = safe_float(apy_list[0].get('maxStepVal', '0'))
    tier1_apy = apy_list[0].get('currentApy', '0')

    print(f"[產品] {coin} 活期 (ID: {product_id})")
    print(f"[第一階梯] 上限: {format_amount(tier1_limit)} {coin}, 年化: {tier1_apy}%")

    # Step 2: 查主帳號餘額
    print(f"\n[Step 2] 查詢主帳號 {coin} 餘額...")
    main_wallet_result = get_spot_assets(coin, 'main')

    main_balance = 0.0
    if main_wallet_result.get('code') == '00000' and main_wallet_result.get('data'):
        wallet_data = main_wallet_result.get('data', [])
        if wallet_data:
            main_balance = safe_float(wallet_data[0].get('available', '0'))

    print(f"[主帳號餘額] {format_amount(main_balance)} {coin}")

    if main_balance <= 0:
        print(f"[跳過] 主帳號沒有 {coin} 餘額")
        return True

    # Step 3: 取得子帳號列表
    config = load_config()
    sub_accounts = []
    for account_id, info in config.get('accounts', {}).items():
        if info.get('type') == 'sub' and info.get('apikey') and info.get('uuid'):
            sub_accounts.append({
                'id': account_id,
                'uuid': info['uuid']
            })

    # 按ID排序
    sub_accounts.sort(key=lambda x: int(x['id']) if x['id'].isdigit() else 0)

    print(f"[子帳號數量] {len(sub_accounts)}")

    # Step 4: 計算分配
    # 主帳號自己留 tier1_limit，剩下的分給子帳號
    main_keep = min(main_balance, tier1_limit)
    available_for_subs = main_balance - main_keep

    # 每個子帳號分 tier1_limit（能分幾個分幾個）
    num_subs_can_fill = int(available_for_subs / tier1_limit) if tier1_limit > 0 else 0
    actual_subs_to_fill = min(num_subs_can_fill, len(sub_accounts))

    print(f"\n[分配計劃]")
    print(f"  主帳號保留: {format_amount(main_keep)} {coin}")
    print(f"  可分發金額: {format_amount(available_for_subs)} {coin}")
    print(f"  可填滿子帳號數: {actual_subs_to_fill}/{len(sub_accounts)}")

    # Step 5: 執行轉帳（主帳號 → 子帳號）
    transfer_tasks = []
    for i in range(actual_subs_to_fill):
        sub = sub_accounts[i]
        transfer_tasks.append({
            'sub_id': sub['id'],
            'sub_uuid': sub['uuid'],
            'amount': tier1_limit
        })

    successful_transfers = []  # 記錄成功轉帳的子帳號

    if transfer_tasks:
        print(f"\n[Step 5] 轉帳到 {len(transfer_tasks)} 個子帳號...")

        executor = ParallelExecutor()

        def do_transfer(task):
            return transfer_to_subaccount(
                coin=coin,
                amount=format_api_amount(task['amount']),
                sub_account_uid=task['sub_uuid'],
                account_key='main'
            )

        tasks_for_executor = [(do_transfer, (t,)) for t in transfer_tasks]
        results = executor.execute_in_batches(tasks_for_executor, batch_size=8, delay_between_batches=1.0)

        for idx, result in results:
            task = transfer_tasks[idx]
            if result.get('code') == '00000':
                print(f"  ✓ 子帳戶{task['sub_id']}: {format_amount(task['amount'])} {coin}")
                successful_transfers.append(task)
            else:
                print(f"  ✗ 子帳戶{task['sub_id']}: {result.get('msg', '未知錯誤')}")

        print(f"[轉帳完成] 成功 {len(successful_transfers)}/{len(transfer_tasks)}")

        if successful_transfers:
            print("[等待] 轉帳結算 (2秒)...")
            time.sleep(2)

    # Step 6: 執行申購（主帳號 + 成功轉帳的子帳號）
    subscribe_tasks = []

    # 主帳號申購
    if main_keep > 0:
        subscribe_tasks.append({
            'account_id': 'main',
            'account_name': '主帳戶',
            'amount': main_keep
        })

    # 子帳號申購
    for task in successful_transfers:
        subscribe_tasks.append({
            'account_id': task['sub_id'],
            'account_name': f"子帳戶{task['sub_id']}",
            'amount': task['amount']
        })

    if subscribe_tasks:
        print(f"\n[Step 6] 申購理財寶 ({len(subscribe_tasks)} 個帳號)...")

        def do_subscribe(task):
            return savings_subscribe(
                product_id, 'flexible',
                format_api_amount(task['amount']),
                account_key=task['account_id']
            )

        # 申購限速較嚴，一直重試直到全部成功（最多 10 輪）
        pending_tasks = subscribe_tasks.copy()
        success_count = 0
        max_retries = 10
        retry_delay = 10  # 重試間隔秒數

        for attempt in range(max_retries + 1):
            if not pending_tasks:
                break

            if attempt > 0:
                print(f"\n[重試 {attempt}] 等待 {retry_delay} 秒後重試 {len(pending_tasks)} 個失敗任務...")
                time.sleep(retry_delay)

            executor = ParallelExecutor()
            tasks_for_executor = [(do_subscribe, (t,)) for t in pending_tasks]
            results = executor.execute_in_batches(tasks_for_executor, batch_size=4, delay_between_batches=1.5)

            failed_tasks = []
            for idx, result in results:
                task = pending_tasks[idx]
                if result.get('code') == '00000':
                    print(f"  ✓ {task['account_name']}: {format_amount(task['amount'])} {coin}")
                    success_count += 1
                else:
                    error_msg = result.get('msg', '未知錯誤')
                    if 'Frequent' in error_msg and attempt < max_retries:
                        failed_tasks.append(task)  # 限速錯誤，加入重試
                    else:
                        print(f"  ✗ {task['account_name']}: {error_msg}")

            pending_tasks = failed_tasks

        print(f"[申購完成] 成功 {success_count}/{len(subscribe_tasks)}")

    print(f"\n[完成] {coin} 處理完畢")
    return True


def show_menu():
    """顯示功能選單"""
    print("\n=== Bitget Flex Manager CLI ===")
    print("1. 初始化 - 完整設定所有子帳戶和API Key")
    print("2. 理財寶管理 - 主子帳戶理財寶批量操作")
    print("3. 轉帳管理 - 主子帳戶間資金轉移")
    print("69. 快速操作 - 一鍵資金分配")
    print("0. 退出")
    print("================================")


def main():
    """主程式"""
    # 啟動時檢查版本更新（異步，不阻塞主程序）
    print("[信息] 檢查版本更新中...")
    check_for_updates()
    
    while True:
        show_menu()
        choice = input("請選擇功能: ").strip()
        
        if choice == '0':
            print("再見!")
            break
        elif choice == '1':
            print("\n[執行] 完整初始化功能...")
            
            # 步驟0: 選擇配置文件
            print("\n[步驟0] 選擇配置文件...")
            test_config = load_config(allow_file_selection=True)
            if not test_config:
                print("[初始化失敗] 無法載入配置文件")
                continue
            
            # 步驟1: 確保有指定數量的子帳戶
            subaccount_success = ensure_target_subaccounts()
            if not subaccount_success:
                print("[初始化失敗] 子帳戶設定失敗")
                continue
            
            # 步驟2: 創建API Key
            print("\n" + "="*50)
            apikey_success = create_apikeys_for_subaccounts()
            
            # 完成總結
            print("\n" + "="*50)
            if apikey_success:
                print("[全部完成] 初始化完成！")
                print("   [OK] 子帳戶設定完成")
                print("   [OK] API Key 設定完成")
                print("   [OK] 配置文件已更新")
            else:
                print("[部分完成] 初始化部分完成")
                print("   [OK] 子帳戶設定完成")
                print("   [ERROR] API Key 創建有問題")
            print("="*50)
        elif choice == '2':
            print("\n[執行] 理財寶管理功能...")
            savings_success = savings_management_workflow()
            if savings_success:
                print("\n[理財寶管理完成]")
            else:
                print("\n[理財寶管理失敗] 請檢查錯誤信息")
        elif choice == '3':
            print("\n[執行] 轉帳管理功能...")
            transfer_success = transfer_management_workflow()
            if transfer_success:
                print("\n[轉帳管理完成]")
            else:
                print("\n[轉帳管理失敗] 請檢查錯誤信息")
        elif choice == '69':
            quick_operations_menu()
        else:
            print("[錯誤] 無效選擇，請重新輸入")


if __name__ == "__main__":
    main()