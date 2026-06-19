# Path: src/services/technical_indicators.py
from typing import List, Dict, Any, Tuple, Optional

def calculate_sma(prices: List[float], period: int) -> List[Optional[float]]:
    sma: List[Optional[float]] = []
    for i in range(len(prices)):
        if i < period - 1:
            sma.append(None)
        else:
            sma.append(sum(prices[i - period + 1 : i + 1]) / period)
    return sma

def calculate_ema(prices: List[float], period: int) -> List[Optional[float]]:
    if not prices:
        return []
    ema: List[Optional[float]] = []
    alpha = 2.0 / (period + 1)
    
    # We compute the first EMA as SMA of the first 'period' elements if possible
    if len(prices) >= period:
        first_sma = sum(prices[:period]) / period
    else:
        first_sma = prices[0]
        
    current_ema = first_sma
    for i in range(len(prices)):
        if i < period - 1:
            ema.append(None)
        elif i == period - 1:
            ema.append(current_ema)
        else:
            current_ema = prices[i] * alpha + current_ema * (1 - alpha)
            ema.append(current_ema)
    return ema

def calculate_rsi(prices: List[float], period: int = 14) -> List[Optional[float]]:
    n = len(prices)
    if n < period + 1:
        return [None] * n
        
    rsi: List[Optional[float]] = [None] * n
    
    deltas = [prices[i] - prices[i-1] for i in range(1, n)]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    if avg_loss == 0:
        rsi[period] = 100.0 if avg_gain > 0 else 50.0
    else:
        rs = avg_gain / avg_loss
        rsi[period] = 100.0 - (100.0 / (1.0 + rs))
        
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
        if avg_loss == 0:
            rsi[i + 1] = 100.0 if avg_gain > 0 else 50.0
        else:
            rs = avg_gain / avg_loss
            rsi[i + 1] = 100.0 - (100.0 / (1.0 + rs))
            
    return rsi

def calculate_macd(
    prices: List[float], 
    fast_period: int = 12, 
    slow_period: int = 26, 
    signal_period: int = 9
) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    n = len(prices)
    if n < slow_period:
        return [None] * n, [None] * n, [None] * n
        
    ema_fast = calculate_ema(prices, fast_period)
    ema_slow = calculate_ema(prices, slow_period)
    
    macd_line: List[Optional[float]] = []
    for f, s in zip(ema_fast, ema_slow):
        if f is None or s is None:
            macd_line.append(None)
        else:
            macd_line.append(f - s)
            
    first_valid_macd_idx = next((i for i, x in enumerate(macd_line) if x is not None), None)
    if first_valid_macd_idx is None:
        return [None] * n, [None] * n, [None] * n
        
    valid_macd = [x for x in macd_line[first_valid_macd_idx:] if x is not None]
    
    # Calculate EMA of MACD line
    ema_signal_valid = calculate_ema(valid_macd, signal_period)
    signal_line: List[Optional[float]] = [None] * first_valid_macd_idx + ema_signal_valid
    
    hist: List[Optional[float]] = []
    for m, sig in zip(macd_line, signal_line):
        if m is None or sig is None:
            hist.append(None)
        else:
            hist.append(m - sig)
            
    # Pad lists if they are slightly shorter due to float precision, but they should match length `n`
    while len(macd_line) < n:
        macd_line.append(None)
    while len(signal_line) < n:
        signal_line.append(None)
    while len(hist) < n:
        hist.append(None)
        
    return macd_line, signal_line, hist

def calculate_dmi(
    highs: List[float], 
    lows: List[float], 
    closes: List[float], 
    period: int = 14
) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    n = len(closes)
    if n < period + 1:
        return [None] * n, [None] * n, [None] * n
        
    plus_di: List[Optional[float]] = [None] * n
    minus_di: List[Optional[float]] = [None] * n
    adx: List[Optional[float]] = [None] * n
    
    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    
    for i in range(1, n):
        h_diff = highs[i] - highs[i-1]
        l_diff = lows[i-1] - lows[i]
        
        if h_diff > l_diff and h_diff > 0:
            plus_dm[i] = h_diff
        else:
            plus_dm[i] = 0.0
            
        if l_diff > h_diff and l_diff > 0:
            minus_dm[i] = l_diff
        else:
            minus_dm[i] = 0.0
            
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        
    smoothed_tr = sum(tr[1:period+1])
    smoothed_plus_dm = sum(plus_dm[1:period+1])
    smoothed_minus_dm = sum(minus_dm[1:period+1])
    
    if smoothed_tr > 0:
        plus_di[period] = 100.0 * (smoothed_plus_dm / smoothed_tr)
        minus_di[period] = 100.0 * (smoothed_minus_dm / smoothed_tr)
    else:
        plus_di[period] = 0.0
        minus_di[period] = 0.0
        
    dx = [0.0] * n
    di_sum = plus_di[period] + minus_di[period]
    dx[period] = 100.0 * (abs(plus_di[period] - minus_di[period]) / di_sum) if di_sum > 0 else 0.0
    
    for i in range(period + 1, n):
        smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr[i]
        smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / period) + plus_dm[i]
        smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / period) + minus_dm[i]
        
        if smoothed_tr > 0:
            plus_di[i] = 100.0 * (smoothed_plus_dm / smoothed_tr)
            minus_di[i] = 100.0 * (smoothed_minus_dm / smoothed_tr)
        else:
            plus_di[i] = 0.0
            minus_di[i] = 0.0
            
        di_sum = plus_di[i] + minus_di[i]
        dx[i] = 100.0 * (abs(plus_di[i] - minus_di[i]) / di_sum) if di_sum > 0 else 0.0
        
    if n >= 2 * period:
        smoothed_dx = sum(dx[period:2*period])
        adx[2*period-1] = smoothed_dx / period
        
        for i in range(2 * period, n):
            smoothed_dx = smoothed_dx - (smoothed_dx / period) + dx[i]
            adx[i] = smoothed_dx / period
            
    return plus_di, minus_di, adx

def compute_all_indicators(klines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    計算 klines 中的所有技術指標並附加回字典中。
    K線列表 klines 必須依日期升序排序。
    """
    if not klines:
        return []
        
    closes = [float(k["close"]) for k in klines]
    highs = [float(k["high"]) for k in klines]
    lows = [float(k["low"]) for k in klines]
    volumes = [float(k["volume"]) for k in klines]
    
    # 價格均線
    ma5 = calculate_sma(closes, 5)
    ma20 = calculate_sma(closes, 20)
    ma60 = calculate_sma(closes, 60)
    
    # 成交量均線
    vol_ma5 = calculate_sma(volumes, 5)
    vol_ma20 = calculate_sma(volumes, 20)
    
    # RSI
    rsi14 = calculate_rsi(closes, 14)
    
    # MACD
    macd_line, signal_line, hist = calculate_macd(closes, 12, 26, 9)
    
    # DMI
    plus_di, minus_di, adx14 = calculate_dmi(highs, lows, closes, 14)
    
    for i, k in enumerate(klines):
        k["ma5"] = ma5[i]
        k["ma20"] = ma20[i]
        k["ma60"] = ma60[i]
        k["vol_ma5"] = vol_ma5[i]
        k["vol_ma20"] = vol_ma20[i]
        k["rsi14"] = rsi14[i]
        k["macd"] = macd_line[i]
        k["macd_signal"] = signal_line[i]
        k["macd_hist"] = hist[i]
        k["plus_di"] = plus_di[i]
        k["minus_di"] = minus_di[i]
        k["adx"] = adx14[i]
        
    return klines
