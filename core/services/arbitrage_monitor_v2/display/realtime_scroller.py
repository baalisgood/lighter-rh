"""
实时滚动区管理器

职责：
- 管理实时价格变化的滚动输出
- 保存最近的消息（用于UI显示）
- 节流控制（500ms），避免阻塞 WebSocket
"""

from core.adapters.exchanges.venues import is_lighter_family
import time
from datetime import datetime
from typing import Dict, Optional, List
from collections import deque


class RealtimeScroller:
    """实时滚动区管理器（保存最近消息，供UI显示）"""
    
    def __init__(self, throttle_ms: float = 500, max_messages: int = 20):
        """
        初始化滚动区管理器
        
        Args:
            throttle_ms: 节流间隔（毫秒）
            max_messages: 保存的最大消息数
        """
        self.throttle_interval = throttle_ms / 1000  # 转换为秒
        self.last_print_time = 0
        
        # 记录上次价格（用于检测变化）
        self.last_prices: Dict[str, Dict[str, float]] = {}  # {exchange: {symbol: price}}
        
        # 🔥 保存最近的消息（用于UI显示）
        self.recent_messages: deque = deque(maxlen=max_messages)
        
        # 🔥 代币去重跟踪（1秒内不显示重复代币）
        self._symbol_last_shown: Dict[str, float] = {}  # {symbol: last_shown_timestamp}
        self._symbol_dedup_seconds: float = 1.0  # 1秒去重窗口
        
        # 启用标志
        self.enabled = True
    
    def print_orderbook_update(
        self,
        exchange: str,
        symbol: str,
        bid_price: float,
        bid_size: float,
        ask_price: float,
        ask_size: float,
        funding_rate: Optional[float] = None  # 🔥 资金费率（8小时，可选）
    ):
        """
        打印订单簿更新（滚动显示）
        
        Args:
            exchange: 交易所名称
            symbol: 交易对
            bid_price: 买一价
            bid_size: 买一量
            ask_price: 卖一价
            ask_size: 卖一量
            funding_rate: 资金费率（8小时，可选）
        """
        if not self.enabled:
            return
        
        # 节流检查
        current_time = time.time()
        if current_time - self.last_print_time < self.throttle_interval:
            return
        
        # 检查价格是否有变化
        key = f"{exchange}_{symbol}"
        last_price = self.last_prices.get(exchange, {}).get(symbol, 0)
        current_price = (bid_price + ask_price) / 2
        
        # 🔥 核心修复：根据交易所调整过滤阈值
        # Lighter更新频率高但变化小，需要更低的阈值
        # 其他交易所保持原有的0.01%阈值
        if last_price > 0:
            price_change_pct = abs(current_price - last_price) / last_price * 100
            
            # 🔥 Lighter使用0.001%阈值（10倍更敏感）
            threshold = 0.001 if is_lighter_family(exchange) else 0.01
            
            if price_change_pct < threshold:  # 变化小于阈值，跳过
                # 🔥 移除debug日志（问题已定位）
                return
        
        # 更新上次价格
        if exchange not in self.last_prices:
            self.last_prices[exchange] = {}
        self.last_prices[exchange][symbol] = current_price
        
        # 计算点差
        spread = ask_price - bid_price
        spread_pct = (spread / bid_price) * 100 if bid_price > 0 else 0
        
        # 格式化时间
        time_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        
        # 🔥 保存消息到队列（供UI显示）
        message = (f"[{time_str}] {exchange:<8} {symbol:<15} | "
                   f"买1: ${bid_price:>10,.2f}×{bid_size:>8,.2f} | "
                   f"卖1: ${ask_price:>10,.2f}×{ask_size:>8,.2f} | "
                   f"点差: ${spread:>6,.2f}({spread_pct:>6.3f}%)")
        self.recent_messages.append(message)
        
        self.last_print_time = current_time
    
    def print_opportunity(
        self,
        symbol: str,
        exchange_buy: str,
        exchange_sell: str,
        price_buy: float,
        price_sell: float,
        spread_pct: float,
        funding_rate_diff: Optional[float] = None  # 🔥 资金费率差（8小时费率差，小数形式）
    ):
        """
        打印套利机会（高亮显示）
        
        Args:
            symbol: 交易对
            exchange_buy: 买入交易所
            exchange_sell: 卖出交易所
            price_buy: 买入价
            price_sell: 卖出价
            spread_pct: 价差百分比
            funding_rate_diff: 资金费率差（8小时费率差，小数形式，如0.0001表示0.01%）
        """
        if not self.enabled:
            return
        
        # 🔥 1秒内不显示重复代币
        current_time = time.time()
        if symbol in self._symbol_last_shown:
            time_since_last_shown = current_time - self._symbol_last_shown[symbol]
            if time_since_last_shown < self._symbol_dedup_seconds:
                # 1秒内已显示过，跳过
                return
        
        # 更新最后显示时间
        self._symbol_last_shown[symbol] = current_time
        
        # 套利机会不节流，立即保存
        time_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        
        # 根据价差大小选择颜色
        if spread_pct >= 0.5:
            emoji = "🔥🔥🔥"
        elif spread_pct >= 0.2:
            emoji = "💰💰"
        else:
            emoji = "💰"
        
        # 🔥 格式化资金费率差（参考v1算法：8小时费率差转换为年化费率差）
        funding_rate_diff_part = ""
        if funding_rate_diff is not None:
            # funding_rate_diff 是8小时费率差（小数形式，如0.0001表示0.01%）
            # 🔥 资金费率差应该永远为正数（绝对值差值）
            rate_diff = abs(funding_rate_diff)  # 确保是正数
            # 8小时差值（百分比）
            diff_8h = float(rate_diff * 100)
            # 年化差值：8小时差值 × 1095
            diff_annual = diff_8h * 1095
            # 费率差永远是正数，不需要符号
            funding_rate_diff_part = f" | 费率差(年化): {diff_annual:.1f}%"
        
        # 🔥 保存套利机会到队列（供UI显示，包含资金费率差）
        message = (f"[{time_str}] {emoji} 套利机会！ {symbol} | "
                   f"{exchange_buy}买${price_buy:,.2f} → {exchange_sell}卖${price_sell:,.2f} | "
                   f"价差: {spread_pct:+.3f}%{funding_rate_diff_part}")
        self.recent_messages.append(message)
    
    def get_recent_messages(self) -> List[str]:
        """
        获取最近的消息列表
        
        Returns:
            最近的消息列表（最新的在最后）
        """
        return list(self.recent_messages)
    
    def clear_messages(self):
        """清空消息队列"""
        self.recent_messages.clear()

