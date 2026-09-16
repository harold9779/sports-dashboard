#!/usr/bin/env python3
"""
赛事赔率贝叶斯分析看板 - 自动更新脚本 v5 (全球联赛版)
修复内容:
  1. 真正的贝叶斯更新: 联赛先验 + 市场赔率似然 (Dirichlet-Multinomial)
  2. Edge = 后验概率 - 1/最高赔率 (有意义的价值指标, 不再恒为0)
  3. 泊松模型: 基于联赛平均进球率 + 赔率强度调整lambda
  4. 凯利公式: 用后验概率 + 最高可获得赔率
  5. 篮球2路分支: 完全跳过平局计算, 不污染数据
  6. 时区: 前端用toLocaleString正确转换北京时间
  7. 赔率离散度: 多公司标准差作为贝叶斯置信度调整
  8. 全球联赛: 动态获取所有足球篮球联赛, 按优先级排序抓取
  9. 配额保护: 剩余请求不足时自动停止, 避免API限流
"""
import json, math, os, re, sys
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.parse import urlencode

# 澳客网爬虫(免费数据源, 无需API Key)
try:
    import okooo_scraper
    OKOOO_AVAILABLE = True
except ImportError:
    OKOOO_AVAILABLE = False
    print('WARNING: okooo_scraper模块未找到, 将仅使用The Odds API')

API_KEY = os.environ.get('ODDS_API_KEY', '')
REPO_DIR = os.environ.get('GITHUB_WORKSPACE', os.path.dirname(os.path.abspath(__file__)))

# ========== 联赛先验数据 (基于历史统计) ==========
LEAGUE_PRIORS = {
    'soccer_epl': {'home': 0.45, 'draw': 0.26, 'away': 0.29, 'lam_h': 1.52, 'lam_a': 1.21},
    'soccer_spain_la_liga': {'home': 0.47, 'draw': 0.25, 'away': 0.28, 'lam_h': 1.58, 'lam_a': 1.15},
    'soccer_germany_bundesliga': {'home': 0.44, 'draw': 0.24, 'away': 0.32, 'lam_h': 1.68, 'lam_a': 1.38},
    'soccer_italy_serie_a': {'home': 0.46, 'draw': 0.27, 'away': 0.27, 'lam_h': 1.42, 'lam_a': 1.12},
    'soccer_france_ligue_one': {'home': 0.45, 'draw': 0.26, 'away': 0.29, 'lam_h': 1.40, 'lam_a': 1.10},
    'soccer_uefa_champs_league': {'home': 0.48, 'draw': 0.25, 'away': 0.27, 'lam_h': 1.62, 'lam_a': 1.20},
    'soccer_efl_champ': {'home': 0.43, 'draw': 0.27, 'away': 0.30, 'lam_h': 1.45, 'lam_a': 1.25},
    'basketball_nba': {'home': 0.58, 'away': 0.42},
    'basketball_euroleague': {'home': 0.55, 'away': 0.45},
}
DEFAULT_FOOTBALL_PRIOR = {'home': 0.45, 'draw': 0.26, 'away': 0.29, 'lam_h': 1.5, 'lam_a': 1.2}
DEFAULT_BASKETBALL_PRIOR = {'home': 0.55, 'away': 0.45}

FOOTBALL_SPORTS = [
    'soccer_epl', 'soccer_spain_la_liga', 'soccer_germany_bundesliga',
    'soccer_italy_serie_a', 'soccer_france_ligue_one',
    'soccer_uefa_champs_league', 'soccer_efl_champ',
]
BASKETBALL_SPORTS = ['basketball_nba', 'basketball_euroleague']

# ========== 联赛优先级 (数字越小越优先) ==========
SPORT_PRIORITY = {
    # 足球 - 顶级联赛（优先级1：最高）
    'soccer_epl': 1, 'soccer_spain_la_liga': 1, 'soccer_germany_bundesliga': 1,
    'soccer_italy_serie_a': 1, 'soccer_france_ligue_one': 1,
    'soccer_uefa_champs_league': 1, 'soccer_world_cup': 1,
    # 足球 - 次顶级（优先级2）
    'soccer_uefa_europa_league': 2, 'soccer_uefa_europa_conference_league': 2,
    'soccer_euro_qual': 2, 'soccer_nations_league': 2,
    # 足球 - 次级联赛（优先级3）
    'soccer_efl_champ': 3, 'soccer_spain_segunda_division': 3,
    'soccer_germany_2_bundesliga': 3, 'soccer_italy_serie_b': 3,
    'soccer_france_ligue_two': 3, 'soccer_netherlands_eredivisie': 3,
    'soccer_portugal_primeira_liga': 3, 'soccer_usa_mls': 3,
    'soccer_mexico_liga_mx': 3, 'soccer_brazil_campeonato_serie_a': 3,
    # 足球 - 三级联赛（优先级4）
    'soccer_england_league_one': 4, 'soccer_russia_premier_league': 4,
    'soccer_turkey_super_lig': 4, 'soccer_belgium_first_div': 4,
    'soccer_scotland_premiership': 4, 'soccer_argentina_primera_division': 4,
    'soccer_china_superleague': 4, 'soccer_japan_j_league': 4,
    # 足球 - 四级联赛（优先级5）
    'soccer_england_league_two': 5, 'soccer_sweden_allsvenskan': 5,
    'soccer_norway_eliteserien': 5, 'soccer_denmark_superliga': 5,
    'soccer_switzerland_superleague': 5, 'soccer_austria_bundesliga': 5,
    'soccer_greece_super_league': 5, 'soccer_south_korea_k_league': 5,
    'soccer_australia_aleague': 5,
    # 足球 - 五级及以下（优先级6-8）
    'soccer_england_conference': 6, 'soccer_spain_segunda_division_b': 6,
    'soccer_germany_3_liga': 6, 'soccer_italy_serie_c': 6,
    'soccer_france_national': 6, 'soccer_netherlands_eerste_divisie': 6,
    'soccer_portugal_liga_pro': 6, 'soccer_mexico_ascenso_mx': 6,
    'soccer_brazil_campeonato_serie_b': 6, 'soccer_argentina_primera_nacional': 6,
    'soccer_japan_j2_league': 6, 'soccer_china_league_one': 6,
    'soccer_saudi_professional_league': 4, 'soccer_uae_pro_league': 5,
    'soccer_qatar_stars_league': 5, 'soccer_egyptian_premier_league': 6,
    'soccer_morocco_botola': 6, 'soccer_south_africa_premier_soccer_league': 6,
    'soccer_nigeria_professional_football_league': 7,
    # 篮球 - 顶级（优先级1）
    'basketball_nba': 1,
    # 篮球 - 次顶级（优先级2）
    'basketball_euroleague': 2, 'basketball_eurocup': 2,
    # 篮球 - 三级（优先级3）
    'basketball_ncaab': 3, 'basketball_spain_acb': 3,
    'basketball_turkey_bsl': 3, 'basketball_greece_heba': 3,
    # 篮球 - 四级（优先级4）
    'basketball_nbagleague': 4, 'basketball_germany_bbl': 4,
    'basketball_france_leguide': 4, 'basketball_italy_seriea': 4,
    'basketball_china_cba': 4, 'basketball_australia_nbl': 4,
    'basketball_russia_vtb': 4, 'basketball_lithuania_lkl': 4,
    # 篮球 - 五级（优先级5）
    'basketball_ncaaw': 5, 'basketball_wnba': 3,
    'basketball_japan_bleague': 5, 'basketball_south_korea_kbl': 5,
    'basketball_philippines_pba': 5,
}
DEFAULT_PRIORITY = 10  # 未知联赛默认优先级

# 配额保护
MIN_REQUESTS_REMAINING = 3  # 剩余请求低于此值时停止抓取(免费版每月500次, 降低阈值以充分利用)
MAX_FOOTBALL_MATCHES = 300   # 足球最多抓取场次（提升至300）
MAX_BASKETBALL_MATCHES = 100  # 篮球最多抓取场次（提升至100）

# 全局剩余请求数
requests_remaining = 999


def fetch_all_sports():
    """获取The Odds API支持的所有活跃联赛"""
    global requests_remaining
    url = f'https://api.the-odds-api.com/v4/sports/?apiKey={API_KEY}'
    try:
        req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urlopen(req, timeout=15) as resp:
            requests_remaining = int(resp.headers.get('x-requests-remaining', '999'))
            data = json.loads(resp.read().decode())
            active = [s for s in data if s.get('active', True)]
            print(f'获取到 {len(data)} 个联赛, 其中活跃 {len(active)} 个 (剩余请求: {requests_remaining})')
            return active
    except Exception as e:
        print(f'获取联赛列表失败: {e}')
        return []


def get_sport_priority(sport_key):
    """获取联赛优先级"""
    return SPORT_PRIORITY.get(sport_key, DEFAULT_PRIORITY)


# ========== 联赛名称中英文映射 ==========
LEAGUE_NAME_CN = {
    # 英格兰
    'EPL': '英超', 'Premier League': '英超',
    'Championship': '英冠', 'EFL Championship': '英冠',
    'England League One': '英甲', 'League One': '英甲',
    'England League Two': '英乙', 'League Two': '英乙',
    'FA Cup': '足总杯', 'EFL Cup': '联赛杯',
    # 西班牙
    'La Liga - Spain': '西甲', 'La Liga': '西甲', 'LaLiga': '西甲',
    'Spain Segunda Division': '西乙', 'Segunda División': '西乙',
    'Copa del Rey': '国王杯',
    # 德国
    'Bundesliga - Germany': '德甲', 'Bundesliga': '德甲',
    'Germany 2. Bundesliga': '德乙', '2. Bundesliga': '德乙',
    'DFB Pokal': '德国杯',
    # 意大利
    'Serie A - Italy': '意甲', 'Serie A': '意甲',
    'Serie B - Italy': '意乙', 'Serie B': '意乙',
    'Coppa Italia': '意大利杯',
    # 法国
    'Ligue 1 - France': '法甲', 'Ligue 1': '法甲',
    'Ligue 2 - France': '法乙', 'Ligue 2': '法乙',
    'Coupe de France': '法国杯',
    # 荷兰
    'Dutch Eredivisie': '荷甲', 'Eredivisie': '荷甲',
    'Eerste Divisie': '荷乙',
    # 葡萄牙
    'Primeira Liga - Portugal': '葡超', 'Primeira Liga': '葡超', 'Portugal Primeira Liga': '葡超',
    'Liga Portugal 2': '葡甲',
    # 欧洲赛事
    'UEFA Champions League': '欧冠', 'Champions League': '欧冠',
    'UEFA Europa League': '欧联', 'Europa League': '欧联',
    'UEFA Europa Conference League': '欧协联',
    'UEFA Super Cup': '欧洲超级杯',
    # 其他欧洲
    'Russia Premier League': '俄超', 'Russian Premier League': '俄超',
    'Turkey Super Lig': '土超', 'Süper Lig': '土超',
    'Belgium First Div A': '比甲', 'Belgian Pro League': '比甲',
    'Scotland Premiership': '苏超', 'Scottish Premiership': '苏超',
    'Sweden Allsvenskan': '瑞典超', 'Allsvenskan': '瑞典超',
    'Norway Eliteserien': '挪威超', 'Eliteserien': '挪威超',
    'Denmark Superliga': '丹超', 'Danish Superliga': '丹超',
    'Switzerland Super League': '瑞士超', 'Swiss Super League': '瑞士超',
    'Austria Bundesliga': '奥甲', 'Austrian Bundesliga': '奥甲',
    'Greece Super League': '希腊超', 'Super League Greece': '希腊超',
    # 美洲
    'USA MLS': '美职联', 'MLS': '美职联', 'Major League Soccer': '美职联',
    'Mexico Liga MX': '墨超', 'Liga MX': '墨超',
    'Brazil Campeonato Serie A': '巴甲', 'Brazil Serie A': '巴甲', 'Campeonato Brasileiro Série A': '巴甲',
    'Argentina Primera Division': '阿甲', 'Argentine Primera División': '阿甲',
    # 亚洲
    'China Superleague': '中超', 'Chinese Super League': '中超', 'CSL': '中超',
    'Japan J League': 'J联赛', 'J1 League': 'J1联赛', 'J.League': 'J联赛',
    'South Korea K League': 'K联赛', 'K League 1': 'K1联赛', 'K League': 'K联赛',
    'Australia A-League': '澳超', 'A-League': '澳超', 'A-League Men': '澳超',
    'Saudi Pro League': '沙特联', 'Saudi Professional League': '沙特联',
    'UAE Pro League': '阿联酋超',
    'Qatar Stars League': '卡塔尔星联',
    # 国际赛事
    'World Cup': '世界杯', 'FIFA World Cup': '世界杯',
    'Euro Qual': '欧洲杯预选赛', 'UEFA Euro Qualifying': '欧洲杯预选赛',
    'European Championship': '欧洲杯', 'UEFA Euro': '欧洲杯',
    'Copa America': '美洲杯', 'Copa América': '美洲杯',
    'Africa Cup of Nations': '非洲杯',
    'Asian Cup': '亚洲杯', 'AFC Asian Cup': '亚洲杯',
    'Gold Cup': '金杯赛', 'CONCACAF Gold Cup': '金杯赛',
    # 篮球
    'NBA': 'NBA', 'National Basketball Association': 'NBA',
    'Basketball Euroleague': '欧洲篮球联赛', 'EuroLeague': '欧洲篮球联赛', 'Turkish Airlines EuroLeague': '欧洲篮球联赛',
    'NCAA Basketball': 'NCAA篮球', 'NCAAB': 'NCAA篮球', 'NCAA Men\'s Basketball': 'NCAA篮球',
    'NBA G League': 'NBA发展联盟', 'NBA G League': 'NBA发展联盟',
    'Spain ACB': '西班牙ACB联赛', 'Liga ACB': '西班牙ACB联赛',
    'Turkey BSL': '土耳其BSL联赛', 'Basketbol Süper Ligi': '土耳其BSL联赛',
    'Germany BBL': '德国BBL联赛', 'Basketball Bundesliga': '德国BBL联赛',
    'France Pro A': '法国Pro A联赛', 'LNB Pro A': '法国Pro A联赛',
    'Italy Serie A': '意大利篮球甲级联赛', 'Serie A (Italy)': '意大利篮球甲级联赛',
    'Greece HEBA': '希腊HEBA联赛', 'Greek Basket League': '希腊篮球联赛',
    'Australia NBL': '澳大利亚NBL', 'NBL': '澳大利亚NBL', 'National Basketball League (Australia)': '澳大利亚NBL',
    'China CBA': 'CBA', 'CBA (China)': 'CBA', 'Chinese Basketball Association': 'CBA',
    'EuroCup Basketball': '欧洲杯篮球联赛', 'EuroCup': '欧洲杯篮球联赛',
    'FIBA Basketball World Cup': '男篮世界杯',
}


def translate_league_name(name):
    """将联赛名称翻译成中文"""
    if not name:
        return name
    # 精确匹配
    if name in LEAGUE_NAME_CN:
        return LEAGUE_NAME_CN[name]
    # 模糊匹配（去除空格、大小写不敏感）
    name_lower = name.lower().replace(' ', '').replace('-', '').replace('.', '')
    for en, cn in LEAGUE_NAME_CN.items():
        en_lower = en.lower().replace(' ', '').replace('-', '').replace('.', '')
        if name_lower == en_lower or name_lower in en_lower or en_lower in name_lower:
            return cn
    # 未匹配则返回原名
    return name

# 贝叶斯先验强度 (相当于N场比赛的信息量)
PRIOR_STRENGTH = 5.0


def fetch_odds(sport_key, regions='eu,uk,us', markets='h2h,totals,spreads', odds_format='decimal', days_ahead=7):
    global requests_remaining
    if requests_remaining < MIN_REQUESTS_REMAINING:
        print(f'  {sport_key}: SKIP (剩余请求不足: {requests_remaining})')
        return []
    # 时间范围: 从现在到未来days_ahead天
    now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    end_iso = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).strftime('%Y-%m-%dT%H:%M:%SZ')
    params = urlencode({
        'apiKey': API_KEY, 'regions': regions, 'markets': markets,
        'oddsFormat': odds_format,
        'commenceTimeFrom': now_iso,
        'commenceTimeTo': end_iso,
    })
    url = f'https://api.the-odds-api.com/v4/sports/{sport_key}/odds/?{params}'
    try:
        req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urlopen(req, timeout=20) as resp:
            requests_remaining = int(resp.headers.get('x-requests-remaining', requests_remaining))
            data = json.loads(resp.read().decode())
            print(f'  {sport_key}: {len(data)} events (remaining: {requests_remaining})')
            return data
    except Exception as e:
        print(f'  {sport_key}: ERROR - {e}')
        return []


# ========== 多源数据去重合并 ==========
def normalize_team_name(name):
    """归一化球队名称用于匹配"""
    if not name:
        return ''
    # 去除空格、标点，统一小写
    name = re.sub(r'[\s\-_./()\[\]{}]', '', name)
    name = name.lower().strip()
    return name


def string_similarity(s1, s2):
    """计算两个字符串的相似度 (0-1)，基于编辑距离"""
    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0
    m, n = len(s1), len(s2)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if s1[i-1] == s2[j-1]:
                dp[i][j] = dp[i-1][j-1]
            else:
                dp[i][j] = 1 + min(dp[i-1][j], dp[i][j-1], dp[i-1][j-1])
    max_len = max(m, n)
    return 1.0 - dp[m][n] / max_len if max_len > 0 else 0.0


def is_same_match(m1, m2, time_tolerance_hours=3, name_similarity_threshold=0.7):
    """判断两场比赛是否为同一场 (支持模糊匹配)"""
    h1 = normalize_team_name(m1.get('home_team', ''))
    a1 = normalize_team_name(m1.get('away_team', ''))
    h2 = normalize_team_name(m2.get('home_team', ''))
    a2 = normalize_team_name(m2.get('away_team', ''))

    if not h1 or not a1 or not h2 or not a2:
        return False

    # 精确匹配 (主客对调也算)
    exact_match = (h1 == h2 and a1 == a2) or (h1 == a2 and a1 == h2)
    if exact_match:
        try:
            t1 = datetime.strptime(m1.get('commence_time', ''), '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
            t2 = datetime.strptime(m2.get('commence_time', ''), '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
            return abs((t1 - t2).total_seconds()) <= time_tolerance_hours * 3600
        except:
            return True

    # 模糊匹配
    sim_hh = string_similarity(h1, h2)
    sim_aa = string_similarity(a1, a2)
    sim_ha = string_similarity(h1, a2)
    sim_ah = string_similarity(a1, h2)

    fuzzy_match = (sim_hh >= name_similarity_threshold and sim_aa >= name_similarity_threshold) or \
                  (sim_ha >= name_similarity_threshold and sim_ah >= name_similarity_threshold)

    if not fuzzy_match:
        return False

    # 模糊匹配时时间容差更大
    try:
        t1 = datetime.strptime(m1.get('commence_time', ''), '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        t2 = datetime.strptime(m2.get('commence_time', ''), '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        return abs((t1 - t2).total_seconds()) <= (time_tolerance_hours + 6) * 3600
    except:
        return True


def merge_bookmakers(m1, m2):
    """合并两场比赛的博彩公司数据"""
    merged = dict(m1)  # 复制m1
    bm_keys = set()
    merged_bookmakers = []

    # 先加入m1的博彩公司
    for bm in m1.get('bookmakers', []):
        bm_keys.add(bm.get('key', ''))
        merged_bookmakers.append(bm)

    # 再加入m2中不重复的博彩公司
    for bm in m2.get('bookmakers', []):
        if bm.get('key', '') not in bm_keys:
            merged_bookmakers.append(bm)
            bm_keys.add(bm.get('key', ''))

    merged['bookmakers'] = merged_bookmakers

    # 保留更多信息的源的联赛名称
    if len(m2.get('sport_title', '')) > len(m1.get('sport_title', '')):
        merged['sport_title'] = m2['sport_title']

    # 标记为合并数据
    merged['merged_sources'] = list(set(
        [m1.get('source', 'theoddsapi')] + [m2.get('source', 'theoddsapi')]
    ))

    return merged


def merge_and_deduplicate(primary_events, secondary_events):
    """
    合并两个数据源的比赛数据，去重
    primary_events: 主数据源(优先保留，如The Odds API)
    secondary_events: 补充数据源(如澳客网爬虫)
    """
    if not secondary_events:
        return primary_events

    merged = list(primary_events)
    added_count = 0
    merged_count = 0

    for sec_event in secondary_events:
        found = False
        for i, prim_event in enumerate(merged):
            if is_same_match(prim_event, sec_event):
                # 同一场比赛，合并博彩公司数据
                merged[i] = merge_bookmakers(prim_event, sec_event)
                merged_count += 1
                found = True
                break
        if not found:
            # 新比赛，添加
            merged.append(sec_event)
            added_count += 1

    print(f'  多源合并: 主源{len(primary_events)}场 + 补充源{len(secondary_events)}场')
    print(f'  合并重复{merged_count}场, 新增{added_count}场, 合计{len(merged)}场')
    return merged


# ========== Shin 去水法 ==========
def shin_dewater(odds_list):
    """
    Shin (1991) 方法去水: 从带水赔率还原真实概率
    支持2路(篮球)和3路(足球)
    """
    n = len(odds_list)
    implied = [1.0 / o for o in odds_list]
    total = sum(implied)

    if n == 2:
        return [imp / total for imp in implied]

    # 3路: Shin迭代法
    z = (total - 1.0) / n
    probs = implied[:]
    for _ in range(100):
        new_probs = []
        for imp in implied:
            p = imp * (1.0 + z * total) / (1.0 + z * n * total)
            new_probs.append(p)
        s = sum(new_probs)
        if abs(s - 1.0) < 1e-10:
            probs = new_probs
            break
        z = z * (1.0 / s)
        probs = new_probs

    s = sum(probs)
    return [p / s for p in probs]


# ========== 凯利公式 ==========
def kelly_fraction(p_model, odds):
    """
    凯利公式: f* = (p * b - q) / b
    b = odds - 1, p = 模型概率, q = 1 - p
    截断 [0, 0.15] 风控
    """
    if odds <= 1.0 or p_model <= 0 or p_model >= 1:
        return 0.0
    b = odds - 1.0
    q = 1.0 - p_model
    f = (p_model * b - q) / b
    f = max(0.0, min(f, 0.15))
    return round(f, 4)


# ========== 泊松比分预测 ==========
def poisson_predict(lam_h, lam_a, max_goals=6):
    """基于泊松分布计算所有比分概率"""
    scores = []
    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            ph = math.exp(-lam_h) * (lam_h ** h) / math.factorial(h)
            pa = math.exp(-lam_a) * (lam_a ** a) / math.factorial(a)
            scores.append({'home': h, 'away': a, 'prob': ph * pa})
    scores.sort(key=lambda x: x['prob'], reverse=True)
    return scores


# ========== 球队Elo系统 ==========
# The Odds API球队名 -> ClubElo球队名 映射表
TEAM_NAME_MAP = {
    # 英超
    'Manchester City': 'Man City', 'Man City': 'Man City',
    'Arsenal': 'Arsenal', 'Liverpool': 'Liverpool',
    'Manchester United': 'Man United', 'Man United': 'Man United',
    'Tottenham Hotspur': 'Tottenham', 'Tottenham': 'Tottenham',
    'Chelsea': 'Chelsea', 'Newcastle United': 'Newcastle', 'Newcastle': 'Newcastle',
    'Aston Villa': 'Aston Villa', 'Brighton and Hove Albion': 'Brighton', 'Brighton': 'Brighton',
    'West Ham United': 'West Ham', 'West Ham': 'West Ham',
    'Crystal Palace': 'Crystal Palace', 'Brentford': 'Brentford',
    'Everton': 'Everton', 'Nottingham Forest': "Nottm Forest",
    'Fulham': 'Fulham', 'Wolverhampton Wanderers': 'Wolves', 'Wolves': 'Wolves',
    'Bournemouth': 'Bournemouth', 'Burnley': 'Burnley',
    'Sheffield United': 'Sheffield Utd', 'Luton Town': 'Luton',
    'Leicester City': 'Leicester', 'Leeds United': 'Leeds',
    'Southampton': 'Southampton', 'Ipswich Town': 'Ipswich',
    'Leicester': 'Leicester', 'Leeds': 'Leeds',
    'Coventry City': 'Coventry', 'Hull City': 'Hull',
    'Sunderland': 'Sunderland', 'Middlesbrough': 'Middlesbrough',
    'Norwich City': 'Norwich', 'Birmingham City': 'Birmingham',
    'Bristol City': 'Bristol City', 'Stoke City': 'Stoke',
    'Swansea City': 'Swansea', 'Watford': 'Watford',
    'Millwall': 'Millwall', 'Preston North End': 'Preston',
    'Blackburn Rovers': 'Blackburn', 'West Bromwich Albion': 'West Brom',
    'Queens Park Rangers': 'QPR', 'Cardiff City': 'Cardiff',
    'Plymouth Argyle': 'Plymouth', 'Rotherham United': 'Rotherham',
    'Huddersfield Town': 'Huddersfield', 'Sheffield Wednesday': 'Sheff Wed',
    'Southampton': 'Southampton', 'Leicester City': 'Leicester',
    'Ipswich Town': 'Ipswich', 'Hull City': 'Hull',
    'Coventry City': 'Coventry', 'Sunderland': 'Sunderland',
    'Middlesbrough': 'Middlesbrough', 'Norwich City': 'Norwich',
    'Birmingham City': 'Birmingham', 'Bristol City': 'Bristol City',
    'Stoke City': 'Stoke', 'Swansea City': 'Swansea',
    'Watford': 'Watford', 'Millwall': 'Millwall',
    'Preston North End': 'Preston', 'Blackburn Rovers': 'Blackburn',
    'West Bromwich Albion': 'West Brom', 'Queens Park Rangers': 'QPR',
    'Cardiff City': 'Cardiff', 'Plymouth Argyle': 'Plymouth',
    'Rotherham United': 'Rotherham', 'Huddersfield Town': 'Huddersfield',
    'Sheffield Wednesday': 'Sheff Wed',
    # 西甲
    'Real Madrid': 'Real Madrid', 'Barcelona': 'Barcelona',
    'Atletico Madrid': 'Atlético', 'Atlético Madrid': 'Atlético',
    'Sevilla': 'Sevilla', 'Real Sociedad': 'Real Sociedad',
    'Villarreal': 'Villarreal', 'Real Betis': 'Betis',
    'Athletic Bilbao': 'Ath Bilbao', 'Valencia': 'Valencia',
    'Getafe': 'Getafe', 'Osasuna': 'Osasuna',
    'Celta Vigo': 'Celta Vigo', 'Rayo Vallecano': 'Rayo Vallecano',
    'Mallorca': 'Mallorca', 'Girona': 'Girona',
    'Almeria': 'Almería', 'Cadiz': 'Cádiz',
    'Elche': 'Elche', 'Espanyol': 'Espanyol',
    'Valladolid': 'Valladolid', 'Alaves': 'Alavés',
    'Las Palmas': 'Las Palmas', 'Granada': 'Granada',
    'Leganes': 'Leganés', 'Eibar': 'Eibar',
    'Sporting Gijon': 'Sporting Gijón', 'Zaragoza': 'Zaragoza',
    'Levante': 'Levante', 'Tenerife': 'Tenerife',
    'Burgos': 'Burgos', 'Racing Santander': 'Racing',
    'Zaragoza': 'Zaragoza', 'Eldense': 'Eldense',
    'Albacete': 'Albacete', 'Villarreal B': 'Villarreal B',
    'Mirandes': 'Mirandés', 'Andorra': 'Andorra',
    'FC Andorra': 'Andorra', 'Cartagena': 'Cartagena',
    'Elche': 'Elche', 'Espanyol': 'Espanyol',
    'Valladolid': 'Valladolid', 'Alaves': 'Alavés',
    'Leganes': 'Leganés', 'Eibar': 'Eibar',
    'Sporting Gijon': 'Sporting Gijón', 'Zaragoza': 'Zaragoza',
    'Levante': 'Levante', 'Tenerife': 'Tenerife',
    'Burgos': 'Burgos', 'Racing Santander': 'Racing',
    'Eldense': 'Eldense', 'Albacete': 'Albacete',
    'Villarreal B': 'Villarreal B', 'Mirandes': 'Mirandés',
    'Andorra': 'Andorra', 'Cartagena': 'Cartagena',
    # 德甲
    'Bayern Munich': 'Bayern München', 'Bayern München': 'Bayern München',
    'Borussia Dortmund': 'Dortmund', 'Dortmund': 'Dortmund',
    'RB Leipzig': 'Leverkusen', 'Bayer Leverkusen': 'Leverkusen',
    'Leverkusen': 'Leverkusen',
    'Union Berlin': 'Union Berlin', 'Freiburg': 'Freiburg',
    'SC Freiburg': 'Freiburg', 'Eintracht Frankfurt': 'Eintracht Frankfurt',
    'Frankfurt': 'Eintracht Frankfurt', 'Wolfsburg': 'Wolfsburg',
    'VfL Wolfsburg': 'Wolfsburg', 'Mainz 05': 'Mainz',
    'Mainz': 'Mainz', 'Borussia Mönchengladbach': 'Mönchengladbach',
    'Borussia Monchengladbach': 'Mönchengladbach',
    'Mönchengladbach': 'Mönchengladbach', 'Hoffenheim': 'Hoffenheim',
    'TSG Hoffenheim': 'Hoffenheim', 'Werder Bremen': 'Werder Bremen',
    'Bremen': 'Werder Bremen', 'Augsburg': 'Augsburg',
    'FC Augsburg': 'Augsburg', 'VfB Stuttgart': 'Stuttgart',
    'Stuttgart': 'Stuttgart', 'Heidenheim': 'Heidenheim',
    '1. FC Heidenheim': 'Heidenheim', 'Darmstadt': 'Darmstadt',
    'SV Darmstadt 98': 'Darmstadt', 'Bochum': 'Bochum',
    'VfL Bochum': 'Bochum', 'Köln': 'Köln',
    'FC Köln': 'Köln', 'Cologne': 'Köln',
    'Schalke 04': 'Schalke', 'Schalke': 'Schalke',
    'Hertha Berlin': 'Hertha', 'Hertha BSC': 'Hertha',
    'Hamburger SV': 'Hamburg', 'Hamburg': 'Hamburg',
    'Hannover 96': 'Hannover', 'Hannover': 'Hannover',
    'Fortuna Düsseldorf': 'Düsseldorf', 'Düsseldorf': 'Düsseldorf',
    'FC St. Pauli': 'St Pauli', 'St. Pauli': 'St Pauli',
    'Holstein Kiel': 'Kiel', 'Kiel': 'Kiel',
    '1. FC Nürnberg': 'Nürnberg', 'Nürnberg': 'Nürnberg',
    'Karlsruher SC': 'Karlsruhe', 'Karlsruhe': 'Karlsruhe',
    'SC Paderborn': 'Paderborn', 'Paderborn': 'Paderborn',
    'SV Elversberg': 'Elversberg', 'Elversberg': 'Elversberg',
    'Hansa Rostock': 'Rostock', 'Rostock': 'Rostock',
    '1. FC Kaiserslautern': 'Kaiserslautern', 'Kaiserslautern': 'Kaiserslautern',
    'Greuther Fürth': 'Fürth', 'Fürth': 'Fürth',
    'Osnabrück': 'Osnabrück', 'Wehen Wiesbaden': 'Wiesbaden',
    # 意甲
    'Inter Milan': 'Internazionale', 'Internazionale': 'Internazionale',
    'Inter': 'Internazionale', 'AC Milan': 'Milan',
    'Milan': 'Milan', 'Juventus': 'Juventus',
    'Napoli': 'Napoli', 'Roma': 'Roma',
    'AS Roma': 'Roma', 'Lazio': 'Lazio',
    'SS Lazio': 'Lazio', 'Atalanta': 'Atalanta',
    'Atalanta BC': 'Atalanta', 'Fiorentina': 'Fiorentina',
    'ACF Fiorentina': 'Fiorentina', 'Bologna': 'Bologna',
    'Bologna FC': 'Bologna', 'Torino': 'Torino',
    'Torino FC': 'Torino', 'Sassuolo': 'Sassuolo',
    'US Sassuolo': 'Sassuolo', 'Monza': 'Monza',
    'AC Monza': 'Monza', 'Udinese': 'Udinese',
    'Udinese Calcio': 'Udinese', 'Cagliari': 'Cagliari',
    'Cagliari Calcio': 'Cagliari', 'Lecce': 'Lecce',
    'US Lecce': 'Lecce', 'Hellas Verona': 'Verona',
    'Verona': 'Verona', 'Empoli': 'Empoli',
    'Empoli FC': 'Empoli', 'Salernitana': 'Salernitana',
    'US Salernitana': 'Salernitana', 'Spezia': 'Spezia',
    'Cremonese': 'Cremonese', 'Sampdoria': 'Sampdoria',
    'UC Sampdoria': 'Sampdoria', 'Parma': 'Parma',
    'Parma Calcio': 'Parma', 'Venezia': 'Venezia',
    'Venezia FC': 'Venezia', 'Cremonese': 'Cremonese',
    'Como': 'Como', 'Como 1907': 'Como',
    'Modena': 'Modena', 'Pisa': 'Pisa',
    'Bari': 'Bari', 'Palermo': 'Palermo',
    'Catanzaro': 'Catanzaro', 'Cittadella': 'Cittadella',
    'Reggiana': 'Reggiana', 'Südtirol': 'Südtirol',
    'Ternana': 'Ternana', 'Cosenza': 'Cosenza',
    'Frosinone': 'Frosinone', 'Lecco': 'Lecco',
    # 法甲
    'Paris Saint Germain': 'Paris SG', 'Paris SG': 'Paris SG',
    'PSG': 'Paris SG', 'Marseille': 'Marseille',
    'Olympique Marseille': 'Marseille', 'Lyon': 'Lyon',
    'Olympique Lyonnais': 'Lyon', 'Monaco': 'Monaco',
    'AS Monaco': 'Monaco', 'Lille': 'Lille',
    'LOSC Lille': 'Lille', 'Rennes': 'Rennes',
    'Stade Rennais': 'Rennes', 'Nice': 'Nice',
    'OGC Nice': 'Nice', 'Nantes': 'Nantes',
    'FC Nantes': 'Nantes', 'Strasbourg': 'Strasbourg',
    'RC Strasbourg': 'Strasbourg', 'Montpellier': 'Montpellier',
    'Montpellier HSC': 'Montpellier', 'Brest': 'Brest',
    'Stade Brestois': 'Brest', 'Lorient': 'Lorient',
    'FC Lorient': 'Lorient', 'Reims': 'Reims',
    'Stade de Reims': 'Reims', 'Toulouse': 'Toulouse',
    'Toulouse FC': 'Toulouse', 'Le Havre': 'Le Havre',
    'Le Havre AC': 'Le Havre', 'Metz': 'Metz',
    'FC Metz': 'Metz', 'Angers': 'Angers',
    'Angers SCO': 'Angers', 'Auxerre': 'Auxerre',
    'AJ Auxerre': 'Auxerre', 'Paris FC': 'Paris FC',
    'Saint-Étienne': 'Saint-Étienne', 'Saint Etienne': 'Saint-Étienne',
    'Bordeaux': 'Bordeaux', 'Lens': 'Lens',
    'RC Lens': 'Lens', 'Caen': 'Caen',
    'SM Caen': 'Caen', 'Guingamp': 'Guingamp',
    'EA Guingamp': 'Guingamp', 'Grenoble': 'Grenoble',
    'Grenoble Foot': 'Grenoble', 'Pau': 'Pau',
    'Pau FC': 'Pau', 'Rodez': 'Rodez',
    'Rodez AF': 'Rodez', 'Quevilly Rouen': 'Quevilly',
    'US Quevilly-Rouen': 'Quevilly', 'Concarneau': 'Concarneau',
    'US Concarneau': 'Concarneau', 'Bastia': 'Bastia',
    'SC Bastia': 'Bastia', 'Dunkerque': 'Dunkerque',
    'USL Dunkerque': 'Dunkerque', 'Valenciennes': 'Valenciennes',
    'Amiens': 'Amiens', 'Amiens SC': 'Amiens',
    'Nîmes': 'Nîmes', 'Nimes': 'Nîmes',
    'Châteauroux': 'Châteauroux', 'Chateauroux': 'Châteauroux',
    'Red Star': 'Red Star', 'Rouen': 'Rouen',
    'FC Rouen': 'Rouen', 'Sochaux': 'Sochaux',
    'FC Sochaux': 'Sochaux', 'Dijon': 'Dijon',
    'DFCO Dijon': 'Dijon', 'Niort': 'Niort',
    'Chamois Niortais': 'Niort', 'Annecy': 'Annecy',
    'FC Annecy': 'Annecy', 'Laval': 'Laval',
    'Stade Lavallois': 'Laval', 'Bourg-Péronnas': 'Bourg-Péronnas',
    # 荷甲
    'Ajax': 'Ajax', 'PSV Eindhoven': 'PSV',
    'PSV': 'PSV', 'Feyenoord': 'Feyenoord',
    'AZ Alkmaar': 'AZ', 'AZ': 'AZ',
    'Alkmaar': 'AZ', 'Twente': 'Twente',
    'FC Twente': 'Twente', 'Utrecht': 'Utrecht',
    'FC Utrecht': 'Utrecht', 'Heerenveen': 'Heerenveen',
    'SC Heerenveen': 'Heerenveen', 'Sparta Rotterdam': 'Sparta',
    'Sparta': 'Sparta', 'NEC Nijmegen': 'NEC',
    'NEC': 'NEC', 'Go Ahead Eagles': 'Go Ahead',
    'Go Ahead Eagles': 'Go Ahead', 'Fortuna Sittard': 'Fortuna',
    'Fortuna Sittard': 'Fortuna', 'Heracles Almelo': 'Heracles',
    'Heracles': 'Heracles', 'RKC Waalwijk': 'RKC',
    'RKC Waalwijk': 'RKC', 'Almere City': 'Almere',
    'Almere City FC': 'Almere', 'Volendam': 'Volendam',
    'FC Volendam': 'Volendam', 'Cambuur': 'Cambuur',
    'SC Cambuur': 'Cambuur', 'Groningen': 'Groningen',
    'FC Groningen': 'Groningen', 'Willem II': 'Willem II',
    'VVV-Venlo': 'VVV', 'De Graafschap': 'De Graafschap',
    'PEC Zwolle': 'Zwolle', 'Zwolle': 'Zwolle',
    'Excelsior': 'Excelsior', 'Excelsior Rotterdam': 'Excelsior',
    # 葡超
    'Porto': 'Porto', 'Benfica': 'Benfica',
    'Sporting CP': 'Sporting', 'Sporting': 'Sporting',
    'Braga': 'Braga', 'SC Braga': 'Braga',
    'Vitoria Guimaraes': 'Vitória Guimarães', 'Vitória Guimarães': 'Vitória Guimarães',
    'Famalicao': 'Famalicão', 'Famalicão': 'Famalicão',
    'Casa Pia': 'Casa Pia', 'Rio Ave': 'Rio Ave',
    'Boavista': 'Boavista', 'Moreirense': 'Moreirense',
    'Estoril': 'Estoril', 'Gil Vicente': 'Gil Vicente',
    'Portimonense': 'Portimonense', 'Chaves': 'Chaves',
    'Vizela': 'Vizela', 'Arouca': 'Arouca',
    'Santa Clara': 'Santa Clara', 'Maritimo': 'Marítimo',
    'Marítimo': 'Marítimo', 'Pacos de Ferreira': 'Paços de Ferreira',
    'Paços de Ferreira': 'Paços de Ferreira',
    'Tondela': 'Tondela', 'Belenenses': 'Belenenses',
    'Nacional': 'Nacional', 'Farense': 'Farense',
    'Leixoes': 'Leixões', 'Leixões': 'Leixões',
    'Penafiel': 'Penafiel', 'Feirense': 'Feirense',
    'Mafra': 'Mafra', 'Oliveirense': 'Oliveirense',
    'Tondela': 'Tondela', 'Vilafranquense': 'Vilafranquense',
    'AVS': 'AVS', 'Uniao de Leiria': 'União de Leiria',
    'União de Leiria': 'União de Leiria', 'B-SAD': 'B-SAD',
    'Académico Viseu': 'Viseu', 'Viseu': 'Viseu',
    'Salgueiros': 'Salgueiros', 'Torreense': 'Torreense',
    'Real SC': 'Real SC', 'Cova da Piedade': 'Cova da Piedade',
    # 其他欧洲
    'Celtic': 'Celtic', 'Rangers': 'Rangers',
    'Aberdeen': 'Aberdeen', 'Hearts': 'Hearts',
    'Heart of Midlothian': 'Hearts', 'Hibernian': 'Hibernian',
    'Dundee United': 'Dundee United', 'Dundee': 'Dundee',
    'Motherwell': 'Motherwell', 'St Mirren': 'St Mirren',
    'Kilmarnock': 'Kilmarnock', 'Ross County': 'Ross County',
    'Livingston': 'Livingston', 'St Johnstone': 'St Johnstone',
    'Partick Thistle': 'Partick Thistle', 'Greenock Morton': 'Morton',
    'Inverness CT': 'Inverness', 'Ayr United': 'Ayr',
    'Queen\'s Park': 'Queen\'s Park', 'Raith Rovers': 'Raith',
    'Dunfermline': 'Dunfermline', 'Arbroath': 'Arbroath',
    'Club Brugge': 'Brugge', 'Brugge': 'Brugge',
    'Anderlecht': 'Anderlecht', 'Union Saint-Gilloise': 'Union SG',
    'Union SG': 'Union SG', 'Antwerp': 'Antwerp',
    'Royal Antwerp': 'Antwerp', 'Gent': 'Gent',
    'KAA Gent': 'Gent', 'Genk': 'Genk',
    'KRC Genk': 'Genk', 'Standard Liege': 'Standard Liège',
    'Standard Liège': 'Standard Liège', 'Mechelen': 'Mechelen',
    'KV Mechelen': 'Mechelen', 'Sint-Truiden': 'Sint-Truiden',
    'STVV': 'Sint-Truiden', 'Kortrijk': 'Kortrijk',
    'KV Kortrijk': 'Kortrijk', 'Cercle Brugge': 'Cercle Brugge',
    'Eupen': 'Eupen', 'KA Eupen': 'Eupen',
    'Oud-Heverlee Leuven': 'OH Leuven', 'OH Leuven': 'OH Leuven',
    'Zulte Waregem': 'Zulte Waregem', 'Beerschot': 'Beerschot',
    'Seraing': 'Seraing', 'RFC Seraing': 'Seraing',
    'Oostende': 'Oostende', 'KV Oostende': 'Oostende',
    'Molenbeek': 'Molenbeek', 'RWDM': 'RWDM',
    'Galatasaray': 'Galatasaray', 'Fenerbahce': 'Fenerbahçe',
    'Fenerbahçe': 'Fenerbahçe', 'Besiktas': 'Beşiktaş',
    'Beşiktaş': 'Beşiktaş', 'Trabzonspor': 'Trabzonspor',
    'Adana Demirspor': 'Adana Demirspor', 'Sivasspor': 'Sivasspor',
    'Konyaspor': 'Konyaspor', 'Antalyaspor': 'Antalyaspor',
    'Kasimpasa': 'Kasımpaşa', 'Kasımpaşa': 'Kasımpaşa',
    'Alanyaspor': 'Alanyaspor', 'Gaziantep': 'Gaziantep',
    'Gaziantep FK': 'Gaziantep', 'Hatayspor': 'Hatayspor',
    'Istanbul Basaksehir': 'Başakşehir', 'Başakşehir': 'Başakşehir',
    'Fatih Karagümrük': 'Karagümrük', 'Karagümrük': 'Karagümrük',
    'Ankaragucu': 'Ankaragücü', 'Ankaragücü': 'Ankaragücü',
    'Samsunspor': 'Samsunspor', 'Rizespor': 'Rizespor',
    'Caykur Rizespor': 'Rizespor', 'Pendikspor': 'Pendikspor',
    'Boluspor': 'Boluspor', 'Samsunspor': 'Samsunspor',
    'Goztepe': 'Göztepe', 'Göztepe': 'Göztepe',
    'Bandirmaspor': 'Bandırmaspor', 'Bandırmaspor': 'Bandırmaspor',
    'Manisa FK': 'Manisa', 'Manisa': 'Manisa',
    'Bodrumspor': 'Bodrumspor', 'Umraniyespor': 'Ümraniyespor',
    'Ümraniyespor': 'Ümraniyespor', 'Altay': 'Altay',
    'Denizlispor': 'Denizlispor', 'Genclerbirligi': 'Gençlerbirliği',
    'Gençlerbirliği': 'Gençlerbirliği', 'Kayserispor': 'Kayserispor',
    'MKE Ankaragücü': 'Ankaragücü', 'Yeni Malatyaspor': 'Malatyaspor',
    'CSKA Moscow': 'CSKA Moskva', 'CSKA Moskva': 'CSKA Moskva',
    'Zenit St Petersburg': 'Zenit', 'Zenit': 'Zenit',
    'Spartak Moscow': 'Spartak Moskva', 'Spartak Moskva': 'Spartak Moskva',
    'Lokomotiv Moscow': 'Lokomotiv Moskva', 'Lokomotiv Moskva': 'Lokomotiv Moskva',
    'Dynamo Moscow': 'Dynamo Moskva', 'Dynamo Moskva': 'Dynamo Moskva',
    'Krasnodar': 'Krasnodar', 'Rostov': 'Rostov',
    'Sochi': 'Sochi', 'Akhmat Grozny': 'Akhmat',
    'Akhmat': 'Akhmat', 'Ural Yekaterinburg': 'Ural',
    'Ural': 'Ural', 'Orenburg': 'Orenburg',
    'Nizhny Novgorod': 'Nizhny Novgorod', 'Khimki': 'Khimki',
    'Torpedo Moscow': 'Torpedo Moskva', 'Torpedo Moskva': 'Torpedo Moskva',
    'Rubin Kazan': 'Rubin', 'Rubin': 'Rubin',
    'Wings of the Soviets': 'Krylia Sovetov', 'Krylia Sovetov': 'Krylia Sovetov',
    'Fakel Voronezh': 'Fakel', 'Fakel': 'Fakel',
    'Baltika Kaliningrad': 'Baltika', 'Baltika': 'Baltika',
    'FC Orenburg': 'Orenburg', 'FC Ural': 'Ural',
    'FC Krasnodar': 'Krasnodar', 'FC Rostov': 'Rostov',
    'FC Sochi': 'Sochi', 'FC Akhmat': 'Akhmat',
    'FC Nizhny Novgorod': 'Nizhny Novgorod', 'FC Khimki': 'Khimki',
    'FC Fakel': 'Fakel', 'FC Baltika': 'Baltika',
    'FC Torpedo': 'Torpedo Moskva', 'FC Rubin': 'Rubin',
    'FC Krylia Sovetov': 'Krylia Sovetov',
    'AIK': 'AIK', 'Malmö FF': 'Malmö', 'Malmö': 'Malmö',
    'Djurgårdens IF': 'Djurgården', 'Djurgården': 'Djurgården',
    'Hammarby': 'Hammarby', 'IFK Göteborg': 'Göteborg',
    'Göteborg': 'Göteborg', 'Kalmar FF': 'Kalmar',
    'Kalmar': 'Kalmar', 'Elfsborg': 'Elfsborg',
    'IF Elfsborg': 'Elfsborg', 'Häcken': 'Häcken',
    'BK Häcken': 'Häcken', 'Sirius': 'Sirius',
    'IK Sirius': 'Sirius', 'Norrköping': 'Norrköping',
    'IFK Norrköping': 'Norrköping', 'Värnamo': 'Värnamo',
    'IFK Värnamo': 'Värnamo', 'Degerfors': 'Degerfors',
    'GIF Sundsvall': 'Sundsvall', 'Sundsvall': 'Sundsvall',
    'Helsingborg': 'Helsingborg', 'Östersund': 'Östersund',
    'Örebro': 'Örebro', 'Falkenberg': 'Falkenberg',
    'Brommapojkarna': 'Brommapojkarna', 'GAIS': 'GAIS',
    'Östers IF': 'Östers', 'Östers': 'Östers',
    'Utsiktens BK': 'Utsikten', 'Västerås SK': 'Västerås',
    'Skövde AIK': 'Skövde', 'Gefle IF': 'Gefle',
    'IK Brage': 'Brage', 'Sandvikens IF': 'Sandviken',
    'Trelleborgs FF': 'Trelleborg', 'Jönköpings Södra': 'Jönköping',
    'Örgryte IS': 'Örgryte', 'Vasalunds IF': 'Vasalund',
    'Sollentuna FK': 'Sollentuna', 'Karlstad BK': 'Karlstad',
    'Bodens BK': 'Boden', 'IFK Luleå': 'Luleå',
    'Team TG FF': 'Team TG', 'Piteå IF': 'Piteå',
    'Assyriska FF': 'Assyriska', 'Syrianska FC': 'Syrianska',
    'Dalkurd FF': 'Dalkurd', 'IK Frej': 'Frej',
    'Nyköpings BIS': 'Nyköping', 'Carlstad United': 'Carlstad',
    'Lunds BK': 'Lund', 'Torns IF': 'Torns',
    'Eskilsminne IF': 'Eskilsminne', 'FC Trollhättan': 'Trollhättan',
    'Oddevold': 'Oddevold', 'Lindome GIF': 'Lindome',
    'Saevits FF': 'Saevits', 'IK Gauthiod': 'Gauthiod',
    'Karlslunds IF': 'Karlslund', 'Enskede IK': 'Enskede',
    'Arameisk-Syrianska': 'Arameisk-Syrianska', 'Konyaspor': 'Konyaspor',
    'Molde': 'Molde', 'Bodø/Glimt': 'Bodø/Glimt',
    'Bodø/Glimt': 'Bodø/Glimt', 'Rosenborg': 'Rosenborg',
    'Viking': 'Viking', 'Brann': 'Brann',
    'SK Brann': 'Brann', 'Lillestrøm': 'Lillestrøm',
    'Lillestrøm SK': 'Lillestrøm', 'Haugesund': 'Haugesund',
    'FK Haugesund': 'Haugesund', 'Strømsgodset': 'Strømsgodset',
    'Sarpsborg 08': 'Sarpsborg', 'Sarpsborg': 'Sarpsborg',
    'Odd': 'Odd', 'Odd Grenland': 'Odd',
    'Tromsø': 'Tromsø', 'Tromsø IL': 'Tromsø',
    'HamKam': 'HamKam', 'Hamarkameratene': 'HamKam',
    'Sandefjord': 'Sandefjord', 'Sandefjord Fotball': 'Sandefjord',
    'Kristiansund': 'Kristiansund', 'Kristiansund BK': 'Kristiansund',
    'Aalesunds': 'Aalesund', 'Aalesunds FK': 'Aalesund',
    'Start': 'Start', 'IK Start': 'Start',
    'Stabæk': 'Stabæk', 'Stabæk Fotball': 'Stabæk',
    'Mjøndalen': 'Mjøndalen', 'Mjøndalen IF': 'Mjøndalen',
    'Jerv': 'Jerv', 'FK Jerv': 'Jerv',
    'Fredrikstad': 'Fredrikstad', 'Fredrikstad FK': 'Fredrikstad',
    'KFUM Oslo': 'KFUM Oslo', 'Kongsvinger': 'Kongsvinger',
    'Kongsvinger IL': 'Kongsvinger', 'Sogndal': 'Sogndal',
    'Sogndal Fotball': 'Sogndal', 'Åsane': 'Åsane',
    'Åsane Fotball': 'Åsane', 'Bryne': 'Bryne',
    'Bryne FK': 'Bryne', 'Ranheim': 'Ranheim',
    'Ranheim TF': 'Ranheim', 'Ull/Kisa': 'Ull/Kisa',
    'Ullensaker/Kisa': 'Ull/Kisa', 'Grorud': 'Grorud',
    'Grorud IL': 'Grorud', 'Raufoss': 'Raufoss',
    'Raufoss IL': 'Raufoss', 'Skeid': 'Skeid',
    'Skeid Fotball': 'Skeid', 'Bærum': 'Bærum',
    'Bærum SK': 'Bærum', 'Asker': 'Asker',
    'Asker Fotball': 'Asker', 'Florø': 'Florø',
    'Florø FK': 'Florø', 'Tromsdalen': 'Tromsdalen',
    'Tromsdalen UIL': 'Tromsdalen', 'Senja': 'Senja',
    'Senja FK': 'Senja', 'Skjervøy': 'Skjervøy',
    'Skjervøy FK': 'Skjervøy', 'Fløya': 'Fløya',
    'Fløya Fotball': 'Fløya', 'Medkila': 'Medkila',
    'Medkila IL': 'Medkila', 'Grand Bodø': 'Grand Bodø',
    'Grand Bodø FK': 'Grand Bodø', 'Innstranden': 'Innstranden',
    'Innstranden FK': 'Innstranden', 'Bossmo & Ytteren': 'Bossmo & Ytteren',
    'Bossmo & Ytteren IL': 'Bossmo & Ytteren', 'Brønnøysund': 'Brønnøysund',
    'Brønnøysund IL': 'Brønnøysund', 'Mosjøen': 'Mosjøen',
    'Mosjøen IL': 'Mosjøen', 'Namsos': 'Namsos',
    'Namsos IL': 'Namsos', 'Verdal': 'Verdal',
    'Verdal IL': 'Verdal', 'Steinkjer': 'Steinkjer',
    'Steinkjer FK': 'Steinkjer', 'Levanger': 'Levanger',
    'Levanger FK': 'Levanger', 'Stjørdals-Blink': 'Stjørdals-Blink',
    'Stjørdals-Blink IL': 'Stjørdals-Blink', 'Rørvik': 'Rørvik',
    'Rørvik IL': 'Rørvik', 'Nardo': 'Nardo',
    'Nardo FK': 'Nardo', 'Orkla': 'Orkla',
    'Orkla FK': 'Orkla', 'KIL/Hemne': 'KIL/Hemne',
    'KIL/Hemne FK': 'KIL/Hemne', 'Buvik': 'Buvik',
    'Buvik IL': 'Buvik', 'Charlottenlund': 'Charlottenlund',
    'Charlottenlund SK': 'Charlottenlund', 'Kolstad': 'Kolstad',
    'Kolstad FK': 'Kolstad', 'Strindheim': 'Strindheim',
    'Strindheim IL': 'Strindheim', 'Ranheim': 'Ranheim',
    'Byåsen': 'Byåsen', 'Byåsen IL': 'Byåsen',
    'Sverresborg': 'Sverresborg', 'Sverresborg IF': 'Sverresborg',
    'Heimdal': 'Heimdal', 'Heimdal FK': 'Heimdal',
    'Vestbyen': 'Vestbyen', 'Vestbyen FK': 'Vestbyen',
    'Aspmyra': 'Aspmyra', 'Aspmyra SK': 'Aspmyra',
    'Glimt': 'Glimt', 'FK Bodø/Glimt': 'Bodø/Glimt',
    'Junkeren': 'Junkeren', 'Junkeren FK': 'Junkeren',
    'Mo': 'Mo', 'Mo IL': 'Mo',
    'Salangen': 'Salangen', 'Salangen IF': 'Salangen',
    'Finnsnes': 'Finnsnes', 'Finnsnes IL': 'Finnsnes',
    'Skarp': 'Skarp', 'Skarp IL': 'Skarp',
    'Lyngen/Karnes': 'Lyngen/Karnes', 'Lyngen/Karnes IL': 'Lyngen/Karnes',
    'Storelva': 'Storelva', 'Storelva IL': 'Storelva',
    'Tromsø': 'Tromsø', 'Tromsø IL': 'Tromsø',
    'Tromsdalen': 'Tromsdalen', 'Tromsdalen UIL': 'Tromsdalen',
    'Fløya': 'Fløya', 'Fløya Fotball': 'Fløya',
    'Medkila': 'Medkila', 'Medkila IL': 'Medkila',
    'Grand Bodø': 'Grand Bodø', 'Grand Bodø FK': 'Grand Bodø',
    'Innstranden': 'Innstranden', 'Innstranden FK': 'Innstranden',
    'Bossmo & Ytteren': 'Bossmo & Ytteren', 'Bossmo & Ytteren IL': 'Bossmo & Ytteren',
    'Brønnøysund': 'Brønnøysund', 'Brønnøysund IL': 'Brønnøysund',
    'Mosjøen': 'Mosjøen', 'Mosjøen IL': 'Mosjøen',
    'Namsos': 'Namsos', 'Namsos IL': 'Namsos',
    'Verdal': 'Verdal', 'Verdal IL': 'Verdal',
    'Steinkjer': 'Steinkjer', 'Steinkjer FK': 'Steinkjer',
    'Levanger': 'Levanger', 'Levanger FK': 'Levanger',
    'Stjørdals-Blink': 'Stjørdals-Blink', 'Stjørdals-Blink IL': 'Stjørdals-Blink',
    'Rørvik': 'Rørvik', 'Rørvik IL': 'Rørvik',
    'Nardo': 'Nardo', 'Nardo FK': 'Nardo',
    'Orkla': 'Orkla', 'Orkla FK': 'Orkla',
    'KIL/Hemne': 'KIL/Hemne', 'KIL/Hemne FK': 'KIL/Hemne',
    'Buvik': 'Buvik', 'Buvik IL': 'Buvik',
    'Charlottenlund': 'Charlottenlund', 'Charlottenlund SK': 'Charlottenlund',
    'Kolstad': 'Kolstad', 'Kolstad FK': 'Kolstad',
    'Strindheim': 'Strindheim', 'Strindheim IL': 'Strindheim',
    'Ranheim': 'Ranheim', 'Ranheim TF': 'Ranheim',
    'Byåsen': 'Byåsen', 'Byåsen IL': 'Byåsen',
    'Sverresborg': 'Sverresborg', 'Sverresborg IF': 'Sverresborg',
    'Heimdal': 'Heimdal', 'Heimdal FK': 'Heimdal',
    'Vestbyen': 'Vestbyen', 'Vestbyen FK': 'Vestbyen',
    'Aspmyra': 'Aspmyra', 'Aspmyra SK': 'Aspmyra',
    'Glimt': 'Glimt', 'FK Bodø/Glimt': 'Bodø/Glimt',
    'Junkeren': 'Junkeren', 'Junkeren FK': 'Junkeren',
    'Mo': 'Mo', 'Mo IL': 'Mo',
    'Salangen': 'Salangen', 'Salangen IF': 'Salangen',
    'Finnsnes': 'Finnsnes', 'Finnsnes IL': 'Finnsnes',
    'Skarp': 'Skarp', 'Skarp IL': 'Skarp',
    'Lyngen/Karnes': 'Lyngen/Karnes', 'Lyngen/Karnes IL': 'Lyngen/Karnes',
    'Storelva': 'Storelva', 'Storelva IL': 'Storelva',
    # 美洲
    'Flamengo': 'Flamengo', 'Palmeiras': 'Palmeiras',
    'Atlético Mineiro': 'Atlético Mineiro', 'Atletico Mineiro': 'Atlético Mineiro',
    'São Paulo': 'São Paulo', 'Sao Paulo': 'São Paulo',
    'Fluminense': 'Fluminense', 'Corinthians': 'Corinthians',
    'Internacional': 'Internacional', 'Grêmio': 'Grêmio',
    'Gremio': 'Grêmio', 'Athletico Paranaense': 'Athletico Paranaense',
    'Santos': 'Santos', 'Vasco da Gama': 'Vasco da Gama',
    'Bahia': 'Bahia', 'Cruzeiro': 'Cruzeiro',
    'Botafogo': 'Botafogo', 'Fortaleza': 'Fortaleza',
    'Ceará': 'Ceará', 'Ceara': 'Ceará',
    'Coritiba': 'Coritiba', 'Goiás': 'Goiás',
    'Goias': 'Goiás', 'América Mineiro': 'América Mineiro',
    'America Mineiro': 'América Mineiro', 'Avaí': 'Avaí',
    'Avai': 'Avaí', 'Cuiabá': 'Cuiabá',
    'Cuiaba': 'Cuiabá', 'Juventude': 'Juventude',
    'Red Bull Bragantino': 'Bragantino', 'Bragantino': 'Bragantino',
    'Atlético Goianiense': 'Atlético Goianiense', 'Atletico Goianiense': 'Atlético Goianiense',
    'Sport Recife': 'Sport Recife', 'Sport': 'Sport Recife',
    'Vitória': 'Vitória', 'Vitoria': 'Vitória',
    'CRB': 'CRB', 'Criciúma': 'Criciúma',
    'Criciuma': 'Criciúma', 'Ponte Preta': 'Ponte Preta',
    'Ceará Sporting Club': 'Ceará', 'Ceará': 'Ceará',
    'CSA': 'CSA', 'Sampaio Corrêa': 'Sampaio Corrêa',
    'Sampaio Correa': 'Sampaio Corrêa', 'Tombense': 'Tombense',
    'Ituano': 'Ituano', 'Mirassol': 'Mirassol',
    'Novorizontino': 'Novorizontino', 'Botafogo-SP': 'Botafogo-SP',
    'Guarani': 'Guarani', 'Vila Nova': 'Vila Nova',
    'CRB': 'CRB', 'Criciúma': 'Criciúma',
    'Criciúma EC': 'Criciúma', 'Ponte Preta': 'Ponte Preta',
    'CSA': 'CSA', 'Sampaio Corrêa': 'Sampaio Corrêa',
    'Sampaio Corrêa FC': 'Sampaio Corrêa', 'Tombense': 'Tombense',
    'Tombense FC': 'Tombense', 'Ituano': 'Ituano',
    'Ituano FC': 'Ituano', 'Mirassol': 'Mirassol',
    'Mirassol FC': 'Mirassol', 'Novorizontino': 'Novorizontino',
    'Novorizontino FC': 'Novorizontino', 'Botafogo-SP': 'Botafogo-SP',
    'Botafogo Futebol Clube (SP)': 'Botafogo-SP', 'Guarani': 'Guarani',
    'Guarani FC': 'Guarani', 'Vila Nova': 'Vila Nova',
    'Vila Nova FC': 'Vila Nova', 'CRB': 'CRB',
    'CRB FC': 'CRB', 'Criciúma': 'Criciúma',
    'Ponte Preta': 'Ponte Preta', 'Ponte Preta FC': 'Ponte Preta',
    'CSA': 'CSA', 'CSA FC': 'CSA',
    'Sampaio Corrêa': 'Sampaio Corrêa', 'Tombense': 'Tombense',
    'Ituano': 'Ituano', 'Mirassol': 'Mirassol',
    'Novorizontino': 'Novorizontino', 'Botafogo-SP': 'Botafogo-SP',
    'Guarani': 'Guarani', 'Vila Nova': 'Vila Nova',
    'CRB': 'CRB', 'Criciúma': 'Criciúma',
    'Ponte Preta': 'Ponte Preta', 'CSA': 'CSA',
    'Sampaio Corrêa': 'Sampaio Corrêa', 'Tombense': 'Tombense',
    'Ituano': 'Ituano', 'Mirassol': 'Mirassol',
    'Novorizontino': 'Novorizontino', 'Botafogo-SP': 'Botafogo-SP',
    'Guarani': 'Guarani', 'Vila Nova': 'Vila Nova',
    'Boca Juniors': 'Boca Juniors', 'River Plate': 'River Plate',
    'Independiente': 'Independiente', 'Racing': 'Racing',
    'San Lorenzo': 'San Lorenzo', 'Estudiantes': 'Estudiantes',
    'Estudiantes de La Plata': 'Estudiantes', 'Rosario Central': 'Rosario Central',
    'Newell\'s Old Boys': 'Newell\'s', 'Newell\'s': 'Newell\'s',
    'Lanús': 'Lanús', 'Lanus': 'Lanús',
    'Defensa y Justicia': 'Defensa y Justicia', 'Colón': 'Colón',
    'Colon': 'Colón', 'Tigre': 'Tigre',
    'Unión': 'Unión', 'Union': 'Unión',
    'Unión de Santa Fe': 'Unión', 'Godoy Cruz': 'Godoy Cruz',
    'Belgrano': 'Belgrano', 'Instituto': 'Instituto',
    'Instituto de Córdoba': 'Instituto', 'Sarmiento': 'Sarmiento',
    'Sarmiento de Junín': 'Sarmiento', 'Central Córdoba': 'Central Córdoba',
    'Central Córdoba de Santiago del Estero': 'Central Córdoba', 'Platense': 'Platense',
    'Banfield': 'Banfield', 'Arsenal de Sarandí': 'Arsenal',
    'Arsenal': 'Arsenal', 'Huracán': 'Huracán',
    'Huracan': 'Huracán', 'Vélez Sarsfield': 'Vélez',
    'Vélez': 'Vélez', 'Velez': 'Vélez',
    'Atlético Tucumán': 'Atlético Tucumán', 'Atletico Tucuman': 'Atlético Tucumán',
    'Patronato': 'Patronato', 'Aldosivi': 'Aldosivi',
    'Colón de Santa Fe': 'Colón', 'Unión de Santa Fe': 'Unión',
    'Estudiantes de La Plata': 'Estudiantes', 'Newell\'s Old Boys': 'Newell\'s',
    'Defensa y Justicia': 'Defensa y Justicia', 'Godoy Cruz Antonio Tomba': 'Godoy Cruz',
    'Instituto Atlético Central Córdoba': 'Instituto', 'Sarmiento de Junín': 'Sarmiento',
    'Central Córdoba de Santiago del Estero': 'Central Córdoba', 'Platense': 'Platense',
    'Club Atlético Banfield': 'Banfield', 'Arsenal de Sarandí': 'Arsenal',
    'Club Atlético Huracán': 'Huracán', 'Vélez Sarsfield': 'Vélez',
    'Atlético Tucumán': 'Atlético Tucumán', 'Patronato de Paraná': 'Patronato',
    'Aldosivi': 'Aldosivi', 'Colón de Santa Fe': 'Colón',
    'Unión de Santa Fe': 'Unión', 'Estudiantes de La Plata': 'Estudiantes',
    'Newell\'s Old Boys': 'Newell\'s', 'Defensa y Justicia': 'Defensa y Justicia',
    'Godoy Cruz Antonio Tomba': 'Godoy Cruz', 'Instituto Atlético Central Córdoba': 'Instituto',
    'Sarmiento de Junín': 'Sarmiento', 'Central Córdoba de Santiago del Estero': 'Central Córdoba',
    'Platense': 'Platense', 'Club Atlético Banfield': 'Banfield',
    'Arsenal de Sarandí': 'Arsenal', 'Club Atlético Huracán': 'Huracán',
    'Vélez Sarsfield': 'Vélez', 'Atlético Tucumán': 'Atlético Tucumán',
    'Patronato de Paraná': 'Patronato', 'Aldosivi': 'Aldosivi',
    # 亚洲
    'Urawa Red Diamonds': 'Urawa', 'Urawa': 'Urawa',
    'Kashima Antlers': 'Kashima', 'Kashima': 'Kashima',
    'Yokohama F. Marinos': 'Yokohama FM', 'Yokohama FM': 'Yokohama FM',
    'Kawasaki Frontale': 'Kawasaki', 'Kawasaki': 'Kawasaki',
    'Sanfrecce Hiroshima': 'Hiroshima', 'Hiroshima': 'Hiroshima',
    'Nagoya Grampus': 'Nagoya', 'Nagoya': 'Nagoya',
    'Hokkaido Consadole Sapporo': 'Sapporo', 'Sapporo': 'Sapporo',
    'Vissel Kobe': 'Vissel Kobe', 'Shimizu S-Pulse': 'Shimizu',
    'Shimizu': 'Shimizu', 'Avispa Fukuoka': 'Fukuoka',
    'Fukuoka': 'Fukuoka', 'Kyoto Sanga': 'Kyoto',
    'Kyoto': 'Kyoto', 'Gamba Osaka': 'Gamba Osaka',
    'Cerezo Osaka': 'Cerezo Osaka', 'Shonan Bellmare': 'Shonan',
    'Shonan': 'Shonan', 'Kashiwa Reysol': 'Kashiwa',
    'Kashiwa': 'Kashiwa', 'Sagan Tosu': 'Tosu',
    'Tosu': 'Tosu', 'FC Tokyo': 'FC Tokyo',
    'Tokyo': 'FC Tokyo', 'Albirex Niigata': 'Niigata',
    'Niigata': 'Niigata', 'Yokohama FC': 'Yokohama FC',
    'Machida Zelvia': 'Machida', 'Machida': 'Machida',
    'Júbilo Iwata': 'Júbilo Iwata', 'Jubilo Iwata': 'Júbilo Iwata',
    'Tokyo Verdy': 'Tokyo Verdy', 'V-Varen Nagasaki': 'Nagasaki',
    'Nagasaki': 'Nagasaki', 'Oita Trinita': 'Oita',
    'Oita': 'Oita', 'Ventforet Kofu': 'Kofu',
    'Kofu': 'Kofu', 'Montedio Yamagata': 'Yamagata',
    'Yamagata': 'Yamagata', 'Mito HollyHock': 'Mito',
    'Mito': 'Mito', 'Zweigen Kanazawa': 'Kanazawa',
    'Kanazawa': 'Kanazawa', 'Renofa Yamaguchi': 'Yamaguchi',
    'Yamaguchi': 'Yamaguchi', 'Blaublitz Akita': 'Akita',
    'Akita': 'Akita', 'Roasso Kumamoto': 'Kumamoto',
    'Kumamoto': 'Kumamoto', 'Tochigi SC': 'Tochigi',
    'Tochigi': 'Tochigi', 'Thespakusatsu Gunma': 'Gunma',
    'Gunma': 'Gunma', 'Omiya Ardija': 'Omiya',
    'Omiya': 'Omiya', 'JEF United Chiba': 'JEF United',
    'JEF United': 'JEF United', 'Fagiano Okayama': 'Okayama',
    'Okayama': 'Okayama', 'Tokushima Vortis': 'Tokushima',
    'Tokushima': 'Tokushima', 'Ehime FC': 'Ehime',
    'Ehime': 'Ehime', 'Iwaki FC': 'Iwaki',
    'Iwaki': 'Iwaki', 'Fujieda MYFC': 'Fujieda',
    'Fujieda': 'Fujieda', 'Kagoshima United': 'Kagoshima',
    'Kagoshima': 'Kagoshima', 'Vanraure Hachinohe': 'Hachinohe',
    'Hachinohe': 'Hachinohe', 'Fukushima United': 'Fukushima',
    'Fukushima': 'Fukushima', 'Gainare Tottori': 'Tottori',
    'Tottori': 'Tottori', 'Kamatamare Sanuki': 'Sanuki',
    'Sanuki': 'Sanuki', 'FC Ryukyu': 'Ryukyu',
    'Ryukyu': 'Ryukyu', 'SC Sagamihara': 'Sagamihara',
    'Sagamihara': 'Sagamihara', 'YSCC Yokohama': 'YSCC Yokohama',
    'Nagano Parceiro': 'Nagano', 'Nagano': 'Nagano',
    'Fujieda MYFC': 'Fujieda', 'Iwaki FC': 'Iwaki',
    'Kagoshima United FC': 'Kagoshima', 'Vanraure Hachinohe': 'Hachinohe',
    'Fukushima United FC': 'Fukushima', 'Gainare Tottori': 'Tottori',
    'Kamatamare Sanuki': 'Sanuki', 'FC Ryukyu': 'Ryukyu',
    'SC Sagamihara': 'Sagamihara', 'YSCC Yokohama': 'YSCC Yokohama',
    'Nagano Parceiro': 'Nagano', 'Fujieda MYFC': 'Fujieda',
    'Iwaki FC': 'Iwaki', 'Kagoshima United FC': 'Kagoshima',
    'Vanraure Hachinohe': 'Hachinohe', 'Fukushima United FC': 'Fukushima',
    'Gainare Tottori': 'Tottori', 'Kamatamare Sanuki': 'Sanuki',
    'FC Ryukyu': 'Ryukyu', 'SC Sagamihara': 'Sagamihara',
    'YSCC Yokohama': 'YSCC Yokohama', 'Nagano Parceiro': 'Nagano',
    'Jeonbuk Hyundai Motors': 'Jeonbuk', 'Jeonbuk': 'Jeonbuk',
    'Ulsan Hyundai': 'Ulsan', 'Ulsan': 'Ulsan',
    'Pohang Steelers': 'Pohang', 'Pohang': 'Pohang',
    'FC Seoul': 'FC Seoul', 'Seoul': 'FC Seoul',
    'Incheon United': 'Incheon', 'Incheon': 'Incheon',
    'Daegu FC': 'Daegu', 'Daegu': 'Daegu',
    'Gwangju FC': 'Gwangju', 'Gwangju': 'Gwangju',
    'Jeju United': 'Jeju', 'Jeju': 'Jeju',
    'Suwon Samsung Bluewings': 'Suwon Bluewings', 'Suwon Bluewings': 'Suwon Bluewings',
    'Gangwon FC': 'Gangwon', 'Gangwon': 'Gangwon',
    'Daejeon Hana Citizen': 'Daejeon', 'Daejeon': 'Daejeon',
    'Suwon FC': 'Suwon FC', 'Busan IPark': 'Busan',
    'Busan': 'Busan', 'Gimcheon Sangmu': 'Gimcheon',
    'Gimcheon': 'Gimcheon', 'FC Anyang': 'Anyang',
    'Anyang': 'Anyang', 'Bucheon FC 1995': 'Bucheon',
    'Bucheon': 'Bucheon', 'Chungnam Asan': 'Chungnam Asan',
    'Seongnam FC': 'Seongnam', 'Seongnam': 'Seongnam',
    'Gyeongnam FC': 'Gyeongnam', 'Gyeongnam': 'Gyeongnam',
    'Jeonnam Dragons': 'Jeonnam', 'Jeonnam': 'Jeonnam',
    'FC Ansan Greeners': 'Ansan', 'Ansan': 'Ansan',
    'Gimpo FC': 'Gimpo', 'Gimpo': 'Gimpo',
    'Cheonan City': 'Cheonan', 'Cheonan': 'Cheonan',
    'Gyeongju KHNP': 'Gyeongju', 'Gyeongju': 'Gyeongju',
    'Gimhae FC': 'Gimhae', 'Gimhae': 'Gimhae',
    'Mokpo City': 'Mokpo', 'Mokpo': 'Mokpo',
    'Changwon City': 'Changwon', 'Changwon': 'Changwon',
    'Daejeon Korail': 'Daejeon Korail', 'Busan Transportation Corporation': 'Busan TC',
    'Paju Citizen': 'Paju', 'Paju': 'Paju',
    'Siheung Citizen': 'Siheung', 'Siheung': 'Siheung',
    'Ulsan Citizen': 'Ulsan Citizen', 'Yangju Citizen': 'Yangju',
    'Yangju': 'Yangju', 'Goyang KH': 'Goyang KH',
    'Pocheon Citizen': 'Pocheon', 'Pocheon': 'Pocheon',
    'Gangneung Citizen': 'Gangneung', 'Gangneung': 'Gangneung',
    'Incheon Ganseok': 'Incheon Ganseok', 'Seoul Nowon United': 'Seoul Nowon',
    'Jeonju Citizen': 'Jeonju', 'Jeonju': 'Jeonju',
    'Hwaseong FC': 'Hwaseong', 'Hwaseong': 'Hwaseong',
    'Pyeongtaek Citizen': 'Pyeongtaek', 'Pyeongtaek': 'Pyeongtaek',
    'Chungju Citizen': 'Chungju', 'Chungju': 'Chungju',
    'Seoul Jungnang': 'Seoul Jungnang', 'Yangpyeong FC': 'Yangpyeong',
    'Yangpyeong': 'Yangpyeong', 'Goyang Citizen': 'Goyang Citizen',
    'Jungnang Chorus': 'Jungnang Chorus', 'Nowon Hummel': 'Nowon Hummel',
    'Seoul United': 'Seoul United', 'Goyang Kookmin Bank': 'Goyang KB',
    'Icheon Citizen': 'Icheon', 'Icheon': 'Icheon',
    'Yeoju FC': 'Yeoju', 'Yeoju': 'Yeoju',
    'Yangju Citizen': 'Yangju', 'Goyang KH': 'Goyang KH',
    'Pocheon Citizen': 'Pocheon', 'Gangneung Citizen': 'Gangneung',
    'Incheon Ganseok': 'Incheon Ganseok', 'Seoul Nowon United': 'Seoul Nowon',
    'Jeonju Citizen': 'Jeonju', 'Hwaseong FC': 'Hwaseong',
    'Pyeongtaek Citizen': 'Pyeongtaek', 'Chungju Citizen': 'Chungju',
    'Seoul Jungnang': 'Seoul Jungnang', 'Yangpyeong FC': 'Yangpyeong',
    'Goyang Citizen': 'Goyang Citizen', 'Jungnang Chorus': 'Jungnang Chorus',
    'Nowon Hummel': 'Nowon Hummel', 'Seoul United': 'Seoul United',
    'Goyang Kookmin Bank': 'Goyang KB', 'Icheon Citizen': 'Icheon',
    'Yeoju FC': 'Yeoju', 'FC Seoul': 'FC Seoul',
    'Shanghai Port': 'Shanghai Port', 'Shanghai SIPG': 'Shanghai Port',
    'Shanghai Shenhua': 'Shanghai Shenhua', 'Beijing Guoan': 'Beijing Guoan',
    'Shandong Taishan': 'Shandong Taishan', 'Shandong Luneng': 'Shandong Taishan',
    'Chengdu Rongcheng': 'Chengdu Rongcheng', 'Zhejiang Professional': 'Zhejiang',
    'Zhejiang': 'Zhejiang', 'Wuhan Three Towns': 'Wuhan Three Towns',
    'Henan Songshan Longmen': 'Henan', 'Henan': 'Henan',
    'Tianjin Jinmen Tiger': 'Tianjin', 'Tianjin': 'Tianjin',
    'Meizhou Hakka': 'Meizhou Hakka', 'Cangzhou Mighty Lions': 'Cangzhou',
    'Cangzhou': 'Cangzhou', 'Qingdao Hainiu': 'Qingdao Hainiu',
    'Nantong Zhiyun': 'Nantong Zhiyun', 'Dalian Pro': 'Dalian Pro',
    'Shenzhen FC': 'Shenzhen', 'Shenzhen': 'Shenzhen',
    'Guangzhou FC': 'Guangzhou', 'Guangzhou Evergrande': 'Guangzhou',
    'Guangzhou': 'Guangzhou', 'Changchun Yatai': 'Changchun Yatai',
    'Wuhan Yangtze River': 'Wuhan', 'Wuhan': 'Wuhan',
    'Guangzhou City': 'Guangzhou City', 'Chongqing Liangjiang Athletic': 'Chongqing',
    'Chongqing': 'Chongqing', 'Qingdao Youth Island': 'Qingdao Youth Island',
    'Xinjiang Tianshan Leopard': 'Xinjiang', 'Xinjiang': 'Xinjiang',
    'Nanjing City': 'Nanjing', 'Nanjing': 'Nanjing',
    'Suzhou Dongwu': 'Suzhou', 'Suzhou': 'Suzhou',
    'Sichuan Jiuniu': 'Sichuan Jiuniu', 'Shenyang Urban': 'Shenyang',
    'Shenyang': 'Shenyang', 'Liaoning Shenyang Urban': 'Shenyang',
    'Zibo Cuju': 'Zibo', 'Zibo': 'Zibo',
    'Shaanxi Chang\'an Athletic': 'Shaanxi', 'Shaanxi': 'Shaanxi',
    'Heilongjiang Ice City': 'Heilongjiang', 'Heilongjiang': 'Heilongjiang',
    'Jiangxi Beidamen': 'Jiangxi', 'Jiangxi': 'Jiangxi',
    'Kunshan FC': 'Kunshan', 'Kunshan': 'Kunshan',
    'Qingdao Hainiu': 'Qingdao Hainiu', 'Nantong Zhiyun': 'Nantong Zhiyun',
    'Shijiazhuang Gongfu': 'Shijiazhuang', 'Shijiazhuang': 'Shijiazhuang',
    'Yanbian Longding': 'Yanbian', 'Yanbian': 'Yanbian',
    'Dandong Tengyue': 'Dandong', 'Dandong': 'Dandong',
    'Dongguan United': 'Dongguan', 'Dongguan': 'Dongguan',
    'Foshan Nanshi': 'Foshan', 'Foshan': 'Foshan',
    'Jinan Xingzhou': 'Jinan', 'Jinan': 'Jinan',
    'Yunnan Yukun': 'Yunnan', 'Yunnan': 'Yunnan',
    'Guangxi Pingguo Haliao': 'Guangxi', 'Guangxi': 'Guangxi',
    'Wuxi Wugo': 'Wuxi', 'Wuxi': 'Wuxi',
    'Nanjing City': 'Nanjing', 'Suzhou Dongwu': 'Suzhou',
    'Sichuan Jiuniu': 'Sichuan Jiuniu', 'Shenyang Urban': 'Shenyang',
    'Zibo Cuju': 'Zibo', 'Shaanxi Chang\'an Athletic': 'Shaanxi',
    'Heilongjiang Ice City': 'Heilongjiang', 'Jiangxi Beidamen': 'Jiangxi',
    'Kunshan FC': 'Kunshan', 'Qingdao Hainiu': 'Qingdao Hainiu',
    'Nantong Zhiyun': 'Nantong Zhiyun', 'Shijiazhuang Gongfu': 'Shijiazhuang',
    'Yanbian Longding': 'Yanbian', 'Dandong Tengyue': 'Dandong',
    'Dongguan United': 'Dongguan', 'Foshan Nanshi': 'Foshan',
    'Jinan Xingzhou': 'Jinan', 'Yunnan Yukun': 'Yunnan',
    'Guangxi Pingguo Haliao': 'Guangxi', 'Wuxi Wugo': 'Wuxi',
    'Nanjing City': 'Nanjing', 'Suzhou Dongwu': 'Suzhou',
    'Sichuan Jiuniu': 'Sichuan Jiuniu', 'Shenyang Urban': 'Shenyang',
    'Zibo Cuju': 'Zibo', 'Shaanxi Chang\'an Athletic': 'Shaanxi',
    'Heilongjiang Ice City': 'Heilongjiang', 'Jiangxi Beidamen': 'Jiangxi',
    'Kunshan FC': 'Kunshan', 'Qingdao Hainiu': 'Qingdao Hainiu',
    'Nantong Zhiyun': 'Nantong Zhiyun', 'Shijiazhuang Gongfu': 'Shijiazhuang',
    'Yanbian Longding': 'Yanbian', 'Dandong Tengyue': 'Dandong',
    'Dongguan United': 'Dongguan', 'Foshan Nanshi': 'Foshan',
    'Jinan Xingzhou': 'Jinan', 'Yunnan Yukun': 'Yunnan',
    'Guangxi Pingguo Haliao': 'Guangxi', 'Wuxi Wugo': 'Wuxi',
    'Melbourne City': 'Melbourne City', 'Sydney FC': 'Sydney FC',
    'Western Sydney Wanderers': 'Western Sydney', 'Western Sydney': 'Western Sydney',
    'Melbourne Victory': 'Melbourne Victory', 'Adelaide United': 'Adelaide United',
    'Brisbane Roar': 'Brisbane Roar', 'Perth Glory': 'Perth Glory',
    'Wellington Phoenix': 'Wellington Phoenix', 'Central Coast Mariners': 'Central Coast',
    'Central Coast': 'Central Coast', 'Newcastle Jets': 'Newcastle Jets',
    'Macarthur FC': 'Macarthur', 'Macarthur': 'Macarthur',
    'Western United': 'Western United', 'Auckland FC': 'Auckland',
    'Auckland': 'Auckland', 'South Melbourne': 'South Melbourne',
    'Sydney Olympic': 'Sydney Olympic', 'APIA Leichhardt': 'APIA Leichhardt',
    'Marconi Stallions': 'Marconi', 'Rockdale Ilinden': 'Rockdale',
    'Manly United': 'Manly', 'Blacktown City': 'Blacktown City',
    'Sutherland Sharks': 'Sutherland', 'Wollongong Wolves': 'Wollongong',
    'Bonnyrigg White Eagles': 'Bonnyrigg', 'Mount Druitt Town Rangers': 'Mount Druitt',
    'NWS Spirit': 'NWS Spirit', 'St George City': 'St George City',
    'Bulls FC Academy': 'Bulls FC Academy', 'Inter Lions': 'Inter Lions',
    'Dulwich Hill': 'Dulwich Hill', 'Hakoah Sydney City East': 'Hakoah',
    'Northern Tigers': 'Northern Tigers', 'Sydney United 58': 'Sydney United',
    'Bankstown City': 'Bankstown City', 'Rydalmere Lions': 'Rydalmere',
    'Canterbury Bankstown': 'Canterbury Bankstown', 'Gladesville Ryde Magic': 'Gladesville',
    'Hills United': 'Hills United', 'Parramatta FC': 'Parramatta',
    'Nepean FC': 'Nepean', 'Hawkesbury City': 'Hawkesbury',
    'Western Rage': 'Western Rage', 'South Coast Flame': 'South Coast Flame',
    'Bankstown United': 'Bankstown United', 'Central Coast United': 'Central Coast United',
    'Newcastle Olympic': 'Newcastle Olympic', 'Charlestown Azzurri': 'Charlestown',
    'Lambton Jaffas': 'Lambton Jaffas', 'Edgeworth Eagles': 'Edgeworth',
    'Broadmeadow Magic': 'Broadmeadow', 'Maitland FC': 'Maitland',
    'Weston Bears': 'Weston Bears', 'Adamstown Rosebud': 'Adamstown',
    'Valentine Phoenix': 'Valentine', 'Lake Macquarie City': 'Lake Macquarie',
    'Cooks Hill United': 'Cooks Hill', 'New Lambton FC': 'New Lambton',
    'Kotara South': 'Kotara South', 'Wallsend FC': 'Wallsend',
    'Hamilton Azzurri': 'Hamilton Azzurri', 'Thornton Redbacks': 'Thornton',
    'Singleton Strikers': 'Singleton', 'Cardiff City': 'Cardiff City',
    'Toronto Awaba Stags': 'Toronto Awaba', 'Garden Suburb': 'Garden Suburb',
    'Kahibah FC': 'Kahibah', 'Raymond Terrace': 'Raymond Terrace',
    'Barnsley': 'Barnsley', 'Barnsley FC': 'Barnsley',
    'Coventry City': 'Coventry', 'Coventry': 'Coventry',
    'Hull City': 'Hull', 'Hull': 'Hull',
    'Ipswich Town': 'Ipswich', 'Ipswich': 'Ipswich',
    'Leicester City': 'Leicester', 'Leicester': 'Leicester',
    'Leeds United': 'Leeds', 'Leeds': 'Leeds',
    'Southampton': 'Southampton', 'Sunderland': 'Sunderland',
    'Watford': 'Watford', 'Norwich City': 'Norwich',
    'Birmingham City': 'Birmingham', 'Bristol City': 'Bristol City',
    'Stoke City': 'Stoke', 'Swansea City': 'Swansea',
    'Millwall': 'Millwall', 'Preston North End': 'Preston',
    'Blackburn Rovers': 'Blackburn', 'West Bromwich Albion': 'West Brom',
    'Queens Park Rangers': 'QPR', 'Cardiff City': 'Cardiff',
    'Plymouth Argyle': 'Plymouth', 'Rotherham United': 'Rotherham',
    'Huddersfield Town': 'Huddersfield', 'Sheffield Wednesday': 'Sheff Wed',
    'Middlesbrough': 'Middlesbrough', 'Bournemouth': 'Bournemouth',
    'Burnley': 'Burnley', 'Sheffield United': 'Sheffield Utd',
    'Luton Town': 'Luton', 'Wolverhampton Wanderers': 'Wolves',
    'Nottingham Forest': "Nottm Forest", 'Fulham': 'Fulham',
    'Brentford': 'Brentford', 'Crystal Palace': 'Crystal Palace',
    'Everton': 'Everton', 'West Ham United': 'West Ham',
    'Newcastle United': 'Newcastle', 'Aston Villa': 'Aston Villa',
    'Brighton and Hove Albion': 'Brighton', 'Chelsea': 'Chelsea',
    'Tottenham Hotspur': 'Tottenham', 'Manchester United': 'Man United',
    'Liverpool': 'Liverpool', 'Arsenal': 'Arsenal',
    'Manchester City': 'Man City', 'Real Madrid': 'Real Madrid',
    'Barcelona': 'Barcelona', 'Atletico Madrid': 'Atlético',
    'Sevilla': 'Sevilla', 'Real Sociedad': 'Real Sociedad',
    'Villarreal': 'Villarreal', 'Real Betis': 'Betis',
    'Athletic Bilbao': 'Ath Bilbao', 'Valencia': 'Valencia',
    'Getafe': 'Getafe', 'Osasuna': 'Osasuna',
    'Celta Vigo': 'Celta Vigo', 'Rayo Vallecano': 'Rayo Vallecano',
    'Mallorca': 'Mallorca', 'Girona': 'Girona',
    'Almeria': 'Almería', 'Cadiz': 'Cádiz',
    'Elche': 'Elche', 'Espanyol': 'Espanyol',
    'Valladolid': 'Valladolid', 'Alaves': 'Alavés',
    'Las Palmas': 'Las Palmas', 'Granada': 'Granada',
    'Leganes': 'Leganés', 'Eibar': 'Eibar',
    'Sporting Gijon': 'Sporting Gijón', 'Zaragoza': 'Zaragoza',
    'Levante': 'Levante', 'Tenerife': 'Tenerife',
    'Burgos': 'Burgos', 'Racing Santander': 'Racing',
    'Bayern Munich': 'Bayern München', 'Borussia Dortmund': 'Dortmund',
    'RB Leipzig': 'Leverkusen', 'Bayer Leverkusen': 'Leverkusen',
    'Union Berlin': 'Union Berlin', 'Freiburg': 'Freiburg',
    'Eintracht Frankfurt': 'Eintracht Frankfurt', 'Wolfsburg': 'Wolfsburg',
    'Mainz 05': 'Mainz', 'Borussia Mönchengladbach': 'Mönchengladbach',
    'Hoffenheim': 'Hoffenheim', 'Werder Bremen': 'Werder Bremen',
    'Augsburg': 'Augsburg', 'VfB Stuttgart': 'Stuttgart',
    'Heidenheim': 'Heidenheim', 'Darmstadt': 'Darmstadt',
    'Bochum': 'Bochum', 'Köln': 'Köln',
    'Schalke 04': 'Schalke', 'Hertha Berlin': 'Hertha',
    'Hamburger SV': 'Hamburg', 'Hannover 96': 'Hannover',
    'Fortuna Düsseldorf': 'Düsseldorf', 'FC St. Pauli': 'St Pauli',
    'Holstein Kiel': 'Kiel', '1. FC Nürnberg': 'Nürnberg',
    'Karlsruher SC': 'Karlsruhe', 'SC Paderborn': 'Paderborn',
    'SV Elversberg': 'Elversberg', 'Hansa Rostock': 'Rostock',
    '1. FC Kaiserslautern': 'Kaiserslautern', 'Greuther Fürth': 'Fürth',
    'Osnabrück': 'Osnabrück', 'Wehen Wiesbaden': 'Wiesbaden',
    'Inter Milan': 'Internazionale', 'AC Milan': 'Milan',
    'Juventus': 'Juventus', 'Napoli': 'Napoli',
    'Roma': 'Roma', 'Lazio': 'Lazio',
    'Atalanta': 'Atalanta', 'Fiorentina': 'Fiorentina',
    'Bologna': 'Bologna', 'Torino': 'Torino',
    'Sassuolo': 'Sassuolo', 'Monza': 'Monza',
    'Udinese': 'Udinese', 'Cagliari': 'Cagliari',
    'Lecce': 'Lecce', 'Hellas Verona': 'Verona',
    'Empoli': 'Empoli', 'Salernitana': 'Salernitana',
    'Spezia': 'Spezia', 'Cremonese': 'Cremonese',
    'Sampdoria': 'Sampdoria', 'Parma': 'Parma',
    'Venezia': 'Venezia', 'Como': 'Como',
    'Modena': 'Modena', 'Pisa': 'Pisa',
    'Bari': 'Bari', 'Palermo': 'Palermo',
    'Catanzaro': 'Catanzaro', 'Cittadella': 'Cittadella',
    'Reggiana': 'Reggiana', 'Südtirol': 'Südtirol',
    'Ternana': 'Ternana', 'Cosenza': 'Cosenza',
    'Frosinone': 'Frosinone', 'Lecco': 'Lecco',
    'Paris Saint Germain': 'Paris SG', 'Marseille': 'Marseille',
    'Lyon': 'Lyon', 'Monaco': 'Monaco',
    'Lille': 'Lille', 'Rennes': 'Rennes',
    'Nice': 'Nice', 'Nantes': 'Nantes',
    'Strasbourg': 'Strasbourg', 'Montpellier': 'Montpellier',
    'Brest': 'Brest', 'Lorient': 'Lorient',
    'Reims': 'Reims', 'Toulouse': 'Toulouse',
    'Le Havre': 'Le Havre', 'Metz': 'Metz',
    'Angers': 'Angers', 'Auxerre': 'Auxerre',
    'Paris FC': 'Paris FC', 'Saint-Étienne': 'Saint-Étienne',
    'Bordeaux': 'Bordeaux', 'Lens': 'Lens',
    'Caen': 'Caen', 'Guingamp': 'Guingamp',
    'Grenoble': 'Grenoble', 'Pau': 'Pau',
    'Rodez': 'Rodez', 'Quevilly Rouen': 'Quevilly',
    'Concarneau': 'Concarneau', 'Bastia': 'Bastia',
    'Dunkerque': 'Dunkerque', 'Valenciennes': 'Valenciennes',
    'Amiens': 'Amiens', 'Nîmes': 'Nîmes',
    'Châteauroux': 'Châteauroux', 'Red Star': 'Red Star',
    'Rouen': 'Rouen', 'Sochaux': 'Sochaux',
    'Dijon': 'Dijon', 'Niort': 'Niort',
    'Annecy': 'Annecy', 'Laval': 'Laval',
    'Bourg-Péronnas': 'Bourg-Péronnas',
    'Ajax': 'Ajax', 'PSV Eindhoven': 'PSV',
    'Feyenoord': 'Feyenoord', 'AZ Alkmaar': 'AZ',
    'Twente': 'Twente', 'Utrecht': 'Utrecht',
    'Heerenveen': 'Heerenveen', 'Sparta Rotterdam': 'Sparta',
    'NEC Nijmegen': 'NEC', 'Go Ahead Eagles': 'Go Ahead',
    'Fortuna Sittard': 'Fortuna', 'Heracles Almelo': 'Heracles',
    'RKC Waalwijk': 'RKC', 'Almere City': 'Almere',
    'Volendam': 'Volendam', 'Cambuur': 'Cambuur',
    'Groningen': 'Groningen', 'Willem II': 'Willem II',
    'VVV-Venlo': 'VVV', 'De Graafschap': 'De Graafschap',
    'PEC Zwolle': 'Zwolle', 'Excelsior': 'Excelsior',
    'Porto': 'Porto', 'Benfica': 'Benfica',
    'Sporting CP': 'Sporting', 'Braga': 'Braga',
    'Vitoria Guimaraes': 'Vitória Guimarães', 'Famalicao': 'Famalicão',
    'Casa Pia': 'Casa Pia', 'Rio Ave': 'Rio Ave',
    'Boavista': 'Boavista', 'Moreirense': 'Moreirense',
    'Estoril': 'Estoril', 'Gil Vicente': 'Gil Vicente',
    'Portimonense': 'Portimonense', 'Chaves': 'Chaves',
    'Vizela': 'Vizela', 'Arouca': 'Arouca',
    'Santa Clara': 'Santa Clara', 'Maritimo': 'Marítimo',
    'Pacos de Ferreira': 'Paços de Ferreira', 'Tondela': 'Tondela',
    'Belenenses': 'Belenenses', 'Nacional': 'Nacional',
    'Farense': 'Farense', 'Leixoes': 'Leixões',
    'Penafiel': 'Penafiel', 'Feirense': 'Feirense',
    'Mafra': 'Mafra', 'Oliveirense': 'Oliveirense',
    'AVS': 'AVS', 'Uniao de Leiria': 'União de Leiria',
    'B-SAD': 'B-SAD', 'Académico Viseu': 'Viseu',
    'Salgueiros': 'Salgueiros', 'Torreense': 'Torreense',
    'Real SC': 'Real SC', 'Cova da Piedade': 'Cova da Piedade',
    'Celtic': 'Celtic', 'Rangers': 'Rangers',
    'Aberdeen': 'Aberdeen', 'Hearts': 'Hearts',
    'Hibernian': 'Hibernian', 'Dundee United': 'Dundee United',
    'Dundee': 'Dundee', 'Motherwell': 'Motherwell',
    'St Mirren': 'St Mirren', 'Kilmarnock': 'Kilmarnock',
    'Ross County': 'Ross County', 'Livingston': 'Livingston',
    'St Johnstone': 'St Johnstone', 'Partick Thistle': 'Partick Thistle',
    'Greenock Morton': 'Morton', 'Inverness CT': 'Inverness',
    'Ayr United': 'Ayr', "Queen's Park": "Queen's Park",
    'Raith Rovers': 'Raith', 'Dunfermline': 'Dunfermline',
    'Arbroath': 'Arbroath', 'Club Brugge': 'Brugge',
    'Anderlecht': 'Anderlecht', 'Union Saint-Gilloise': 'Union SG',
    'Antwerp': 'Antwerp', 'Gent': 'Gent',
    'Genk': 'Genk', 'Standard Liege': 'Standard Liège',
    'Mechelen': 'Mechelen', 'Sint-Truiden': 'Sint-Truiden',
    'Kortrijk': 'Kortrijk', 'Cercle Brugge': 'Cercle Brugge',
    'Eupen': 'Eupen', 'Oud-Heverlee Leuven': 'OH Leuven',
    'Zulte Waregem': 'Zulte Waregem', 'Beerschot': 'Beerschot',
    'Seraing': 'Seraing', 'Oostende': 'Oostende',
    'Molenbeek': 'Molenbeek', 'RWDM': 'RWDM',
    'Galatasaray': 'Galatasaray', 'Fenerbahce': 'Fenerbahçe',
    'Besiktas': 'Beşiktaş', 'Trabzonspor': 'Trabzonspor',
    'Adana Demirspor': 'Adana Demirspor', 'Sivasspor': 'Sivasspor',
    'Konyaspor': 'Konyaspor', 'Antalyaspor': 'Antalyaspor',
    'Kasimpasa': 'Kasımpaşa', 'Alanyaspor': 'Alanyaspor',
    'Gaziantep': 'Gaziantep', 'Hatayspor': 'Hatayspor',
    'Istanbul Basaksehir': 'Başakşehir', 'Fatih Karagümrük': 'Karagümrük',
    'Ankaragucu': 'Ankaragücü', 'Samsunspor': 'Samsunspor',
    'Rizespor': 'Rizespor', 'Pendikspor': 'Pendikspor',
    'Boluspor': 'Boluspor', 'Goztepe': 'Göztepe',
    'Bandirmaspor': 'Bandırmaspor', 'Manisa FK': 'Manisa',
    'Bodrumspor': 'Bodrumspor', 'Umraniyespor': 'Ümraniyespor',
    'Altay': 'Altay', 'Denizlispor': 'Denizlispor',
    'Genclerbirligi': 'Gençlerbirliği', 'Kayserispor': 'Kayserispor',
    'Yeni Malatyaspor': 'Malatyaspor', 'CSKA Moscow': 'CSKA Moskva',
    'Zenit St Petersburg': 'Zenit', 'Spartak Moscow': 'Spartak Moskva',
    'Lokomotiv Moscow': 'Lokomotiv Moskva', 'Dynamo Moscow': 'Dynamo Moskva',
    'Krasnodar': 'Krasnodar', 'Rostov': 'Rostov',
    'Sochi': 'Sochi', 'Akhmat Grozny': 'Akhmat',
    'Ural Yekaterinburg': 'Ural', 'Orenburg': 'Orenburg',
    'Nizhny Novgorod': 'Nizhny Novgorod', 'Khimki': 'Khimki',
    'Torpedo Moscow': 'Torpedo Moskva', 'Rubin Kazan': 'Rubin',
    'Wings of the Soviets': 'Krylia Sovetov', 'Fakel Voronezh': 'Fakel',
    'Baltika Kaliningrad': 'Baltika', 'AIK': 'AIK',
    'Malmö FF': 'Malmö', 'Djurgårdens IF': 'Djurgården',
    'Hammarby': 'Hammarby', 'IFK Göteborg': 'Göteborg',
    'Kalmar FF': 'Kalmar', 'Elfsborg': 'Elfsborg',
    'BK Häcken': 'Häcken', 'Sirius': 'Sirius',
    'IFK Norrköping': 'Norrköping', 'IFK Värnamo': 'Värnamo',
    'Degerfors': 'Degerfors', 'GIF Sundsvall': 'Sundsvall',
    'Helsingborg': 'Helsingborg', 'Östersund': 'Östersund',
    'Örebro': 'Örebro', 'Falkenberg': 'Falkenberg',
    'Brommapojkarna': 'Brommapojkarna', 'GAIS': 'GAIS',
    'Östers IF': 'Östers', 'Utsiktens BK': 'Utsikten',
    'Västerås SK': 'Västerås', 'Skövde AIK': 'Skövde',
    'Gefle IF': 'Gefle', 'IK Brage': 'Brage',
    'Sandvikens IF': 'Sandviken', 'Trelleborgs FF': 'Trelleborg',
    'Jönköpings Södra': 'Jönköping', 'Örgryte IS': 'Örgryte',
    'Vasalunds IF': 'Vasalund', 'Sollentuna FK': 'Sollentuna',
    'Karlstad BK': 'Karlstad', 'Bodens BK': 'Boden',
    'IFK Luleå': 'Luleå', 'Team TG FF': 'Team TG',
    'Piteå IF': 'Piteå', 'Assyriska FF': 'Assyriska',
    'Syrianska FC': 'Syrianska', 'Dalkurd FF': 'Dalkurd',
    'IK Frej': 'Frej', 'Nyköpings BIS': 'Nyköping',
    'Carlstad United': 'Carlstad', 'Lunds BK': 'Lund',
    'Torns IF': 'Torns', 'Eskilsminne IF': 'Eskilsminne',
    'FC Trollhättan': 'Trollhättan', 'Oddevold': 'Oddevold',
    'Lindome GIF': 'Lindome', 'Saevits FF': 'Saevits',
    'IK Gauthiod': 'Gauthiod', 'Karlslunds IF': 'Karlslund',
    'Enskede IK': 'Enskede', 'Arameisk-Syrianska': 'Arameisk-Syrianska',
    'Molde': 'Molde', 'Bodø/Glimt': 'Bodø/Glimt',
    'Rosenborg': 'Rosenborg', 'Viking': 'Viking',
    'Brann': 'Brann', 'Lillestrøm': 'Lillestrøm',
    'Haugesund': 'Haugesund', 'Strømsgodset': 'Strømsgodset',
    'Sarpsborg 08': 'Sarpsborg', 'Odd': 'Odd',
    'Tromsø': 'Tromsø', 'HamKam': 'HamKam',
    'Sandefjord': 'Sandefjord', 'Kristiansund': 'Kristiansund',
    'Aalesunds': 'Aalesund', 'Start': 'Start',
    'Stabæk': 'Stabæk', 'Mjøndalen': 'Mjøndalen',
    'Jerv': 'Jerv', 'Fredrikstad': 'Fredrikstad',
    'KFUM Oslo': 'KFUM Oslo', 'Kongsvinger': 'Kongsvinger',
    'Sogndal': 'Sogndal', 'Åsane': 'Åsane',
    'Bryne': 'Bryne', 'Ranheim': 'Ranheim',
    'Ull/Kisa': 'Ull/Kisa', 'Grorud': 'Grorud',
    'Raufoss': 'Raufoss', 'Skeid': 'Skeid',
    'Bærum': 'Bærum', 'Asker': 'Asker',
    'Florø': 'Florø', 'Tromsdalen': 'Tromsdalen',
    'Senja': 'Senja', 'Skjervøy': 'Skjervøy',
    'Fløya': 'Fløya', 'Medkila': 'Medkila',
    'Grand Bodø': 'Grand Bodø', 'Innstranden': 'Innstranden',
    'Bossmo & Ytteren': 'Bossmo & Ytteren', 'Brønnøysund': 'Brønnøysund',
    'Mosjøen': 'Mosjøen', 'Namsos': 'Namsos',
    'Verdal': 'Verdal', 'Steinkjer': 'Steinkjer',
    'Levanger': 'Levanger', 'Stjørdals-Blink': 'Stjørdals-Blink',
    'Rørvik': 'Rørvik', 'Nardo': 'Nardo',
    'Orkla': 'Orkla', 'KIL/Hemne': 'KIL/Hemne',
    'Buvik': 'Buvik', 'Charlottenlund': 'Charlottenlund',
    'Kolstad': 'Kolstad', 'Strindheim': 'Strindheim',
    'Byåsen': 'Byåsen', 'Sverresborg': 'Sverresborg',
    'Heimdal': 'Heimdal', 'Vestbyen': 'Vestbyen',
    'Aspmyra': 'Aspmyra', 'Glimt': 'Glimt',
    'Junkeren': 'Junkeren', 'Mo': 'Mo',
    'Salangen': 'Salangen', 'Finnsnes': 'Finnsnes',
    'Skarp': 'Skarp', 'Lyngen/Karnes': 'Lyngen/Karnes',
    'Storelva': 'Storelva', 'Flamengo': 'Flamengo',
    'Palmeiras': 'Palmeiras', 'Atlético Mineiro': 'Atlético Mineiro',
    'São Paulo': 'São Paulo', 'Fluminense': 'Fluminense',
    'Corinthians': 'Corinthians', 'Internacional': 'Internacional',
    'Grêmio': 'Grêmio', 'Athletico Paranaense': 'Athletico Paranaense',
    'Santos': 'Santos', 'Vasco da Gama': 'Vasco da Gama',
    'Bahia': 'Bahia', 'Cruzeiro': 'Cruzeiro',
    'Botafogo': 'Botafogo', 'Fortaleza': 'Fortaleza',
    'Ceará': 'Ceará', 'Coritiba': 'Coritiba',
    'Goiás': 'Goiás', 'América Mineiro': 'América Mineiro',
    'Avaí': 'Avaí', 'Cuiabá': 'Cuiabá',
    'Juventude': 'Juventude', 'Red Bull Bragantino': 'Bragantino',
    'Atlético Goianiense': 'Atlético Goianiense', 'Sport Recife': 'Sport Recife',
    'Vitória': 'Vitória', 'CRB': 'CRB',
    'Criciúma': 'Criciúma', 'Ponte Preta': 'Ponte Preta',
    'CSA': 'CSA', 'Sampaio Corrêa': 'Sampaio Corrêa',
    'Tombense': 'Tombense', 'Ituano': 'Ituano',
    'Mirassol': 'Mirassol', 'Novorizontino': 'Novorizontino',
    'Botafogo-SP': 'Botafogo-SP', 'Guarani': 'Guarani',
    'Vila Nova': 'Vila Nova', 'Boca Juniors': 'Boca Juniors',
    'River Plate': 'River Plate', 'Independiente': 'Independiente',
    'Racing': 'Racing', 'San Lorenzo': 'San Lorenzo',
    'Estudiantes': 'Estudiantes', 'Rosario Central': 'Rosario Central',
    "Newell's Old Boys": "Newell's", 'Lanús': 'Lanús',
    'Defensa y Justicia': 'Defensa y Justicia', 'Colón': 'Colón',
    'Tigre': 'Tigre', 'Unión': 'Unión',
    'Godoy Cruz': 'Godoy Cruz', 'Belgrano': 'Belgrano',
    'Instituto': 'Instituto', 'Sarmiento': 'Sarmiento',
    'Central Córdoba': 'Central Córdoba', 'Platense': 'Platense',
    'Banfield': 'Banfield', 'Arsenal': 'Arsenal',
    'Huracán': 'Huracán', 'Vélez Sarsfield': 'Vélez',
    'Atlético Tucumán': 'Atlético Tucumán', 'Patronato': 'Patronato',
    'Aldosivi': 'Aldosivi', 'Urawa Red Diamonds': 'Urawa',
    'Kashima Antlers': 'Kashima', 'Yokohama F. Marinos': 'Yokohama FM',
    'Kawasaki Frontale': 'Kawasaki', 'Sanfrecce Hiroshima': 'Hiroshima',
    'Nagoya Grampus': 'Nagoya', 'Hokkaido Consadole Sapporo': 'Sapporo',
    'Vissel Kobe': 'Vissel Kobe', 'Shimizu S-Pulse': 'Shimizu',
    'Avispa Fukuoka': 'Fukuoka', 'Kyoto Sanga': 'Kyoto',
    'Gamba Osaka': 'Gamba Osaka', 'Cerezo Osaka': 'Cerezo Osaka',
    'Shonan Bellmare': 'Shonan', 'Kashiwa Reysol': 'Kashiwa',
    'Sagan Tosu': 'Tosu', 'FC Tokyo': 'FC Tokyo',
    'Albirex Niigata': 'Niigata', 'Yokohama FC': 'Yokohama FC',
    'Machida Zelvia': 'Machida', 'Júbilo Iwata': 'Júbilo Iwata',
    'Tokyo Verdy': 'Tokyo Verdy', 'V-Varen Nagasaki': 'Nagasaki',
    'Oita Trinita': 'Oita', 'Ventforet Kofu': 'Kofu',
    'Montedio Yamagata': 'Yamagata', 'Mito HollyHock': 'Mito',
    'Zweigen Kanazawa': 'Kanazawa', 'Renofa Yamaguchi': 'Yamaguchi',
    'Blaublitz Akita': 'Akita', 'Roasso Kumamoto': 'Kumamoto',
    'Tochigi SC': 'Tochigi', 'Thespakusatsu Gunma': 'Gunma',
    'Omiya Ardija': 'Omiya', 'JEF United Chiba': 'JEF United',
    'Fagiano Okayama': 'Okayama', 'Tokushima Vortis': 'Tokushima',
    'Ehime FC': 'Ehime', 'Iwaki FC': 'Iwaki',
    'Fujieda MYFC': 'Fujieda', 'Kagoshima United': 'Kagoshima',
    'Vanraure Hachinohe': 'Hachinohe', 'Fukushima United': 'Fukushima',
    'Gainare Tottori': 'Tottori', 'Kamatamare Sanuki': 'Sanuki',
    'FC Ryukyu': 'Ryukyu', 'SC Sagamihara': 'Sagamihara',
    'YSCC Yokohama': 'YSCC Yokohama', 'Nagano Parceiro': 'Nagano',
    'Jeonbuk Hyundai Motors': 'Jeonbuk', 'Ulsan Hyundai': 'Ulsan',
    'Pohang Steelers': 'Pohang', 'FC Seoul': 'FC Seoul',
    'Incheon United': 'Incheon', 'Daegu FC': 'Daegu',
    'Gwangju FC': 'Gwangju', 'Jeju United': 'Jeju',
    'Suwon Samsung Bluewings': 'Suwon Bluewings', 'Gangwon FC': 'Gangwon',
    'Daejeon Hana Citizen': 'Daejeon', 'Suwon FC': 'Suwon FC',
    'Busan IPark': 'Busan', 'Gimcheon Sangmu': 'Gimcheon',
    'FC Anyang': 'Anyang', 'Bucheon FC 1995': 'Bucheon',
    'Chungnam Asan': 'Chungnam Asan', 'Seongnam FC': 'Seongnam',
    'Gyeongnam FC': 'Gyeongnam', 'Jeonnam Dragons': 'Jeonnam',
    'FC Ansan Greeners': 'Ansan', 'Gimpo FC': 'Gimpo',
    'Cheonan City': 'Cheonan', 'Gyeongju KHNP': 'Gyeongju',
    'Gimhae FC': 'Gimhae', 'Mokpo City': 'Mokpo',
    'Changwon City': 'Changwon', 'Daejeon Korail': 'Daejeon Korail',
    'Busan Transportation Corporation': 'Busan TC', 'Paju Citizen': 'Paju',
    'Siheung Citizen': 'Siheung', 'Ulsan Citizen': 'Ulsan Citizen',
    'Yangju Citizen': 'Yangju', 'Goyang KH': 'Goyang KH',
    'Pocheon Citizen': 'Pocheon', 'Gangneung Citizen': 'Gangneung',
    'Incheon Ganseok': 'Incheon Ganseok', 'Seoul Nowon United': 'Seoul Nowon',
    'Jeonju Citizen': 'Jeonju', 'Hwaseong FC': 'Hwaseong',
    'Pyeongtaek Citizen': 'Pyeongtaek', 'Chungju Citizen': 'Chungju',
    'Seoul Jungnang': 'Seoul Jungnang', 'Yangpyeong FC': 'Yangpyeong',
    'Goyang Citizen': 'Goyang Citizen', 'Jungnang Chorus': 'Jungnang Chorus',
    'Nowon Hummel': 'Nowon Hummel', 'Seoul United': 'Seoul United',
    'Goyang Kookmin Bank': 'Goyang KB', 'Icheon Citizen': 'Icheon',
    'Yeoju FC': 'Yeoju', 'Shanghai Port': 'Shanghai Port',
    'Shanghai Shenhua': 'Shanghai Shenhua', 'Beijing Guoan': 'Beijing Guoan',
    'Shandong Taishan': 'Shandong Taishan', 'Chengdu Rongcheng': 'Chengdu Rongcheng',
    'Zhejiang Professional': 'Zhejiang', 'Wuhan Three Towns': 'Wuhan Three Towns',
    'Henan Songshan Longmen': 'Henan', 'Tianjin Jinmen Tiger': 'Tianjin',
    'Meizhou Hakka': 'Meizhou Hakka', 'Cangzhou Mighty Lions': 'Cangzhou',
    'Qingdao Hainiu': 'Qingdao Hainiu', 'Nantong Zhiyun': 'Nantong Zhiyun',
    'Dalian Pro': 'Dalian Pro', 'Shenzhen FC': 'Shenzhen',
    'Guangzhou FC': 'Guangzhou', 'Changchun Yatai': 'Changchun Yatai',
    'Wuhan Yangtze River': 'Wuhan', 'Guangzhou City': 'Guangzhou City',
    'Chongqing Liangjiang Athletic': 'Chongqing', 'Qingdao Youth Island': 'Qingdao Youth Island',
    'Xinjiang Tianshan Leopard': 'Xinjiang', 'Nanjing City': 'Nanjing',
    'Suzhou Dongwu': 'Suzhou', 'Sichuan Jiuniu': 'Sichuan Jiuniu',
    'Shenyang Urban': 'Shenyang', 'Zibo Cuju': 'Zibo',
    'Shaanxi Chang\'an Athletic': 'Shaanxi', 'Heilongjiang Ice City': 'Heilongjiang',
    'Jiangxi Beidamen': 'Jiangxi', 'Kunshan FC': 'Kunshan',
    'Shijiazhuang Gongfu': 'Shijiazhuang', 'Yanbian Longding': 'Yanbian',
    'Dandong Tengyue': 'Dandong', 'Dongguan United': 'Dongguan',
    'Foshan Nanshi': 'Foshan', 'Jinan Xingzhou': 'Jinan',
    'Yunnan Yukun': 'Yunnan', 'Guangxi Pingguo Haliao': 'Guangxi',
    'Wuxi Wugo': 'Wuxi', 'Melbourne City': 'Melbourne City',
    'Sydney FC': 'Sydney FC', 'Western Sydney Wanderers': 'Western Sydney',
    'Melbourne Victory': 'Melbourne Victory', 'Adelaide United': 'Adelaide United',
    'Brisbane Roar': 'Brisbane Roar', 'Perth Glory': 'Perth Glory',
    'Wellington Phoenix': 'Wellington Phoenix', 'Central Coast Mariners': 'Central Coast',
    'Newcastle Jets': 'Newcastle Jets', 'Macarthur FC': 'Macarthur',
    'Western United': 'Western United', 'Auckland FC': 'Auckland',
    'South Melbourne': 'South Melbourne', 'Sydney Olympic': 'Sydney Olympic',
    'APIA Leichhardt': 'APIA Leichhardt', 'Marconi Stallions': 'Marconi',
    'Rockdale Ilinden': 'Rockdale', 'Manly United': 'Manly',
    'Blacktown City': 'Blacktown City', 'Sutherland Sharks': 'Sutherland',
    'Wollongong Wolves': 'Wollongong', 'Bonnyrigg White Eagles': 'Bonnyrigg',
    'Mount Druitt Town Rangers': 'Mount Druitt', 'NWS Spirit': 'NWS Spirit',
    'St George City': 'St George City', 'Bulls FC Academy': 'Bulls FC Academy',
    'Inter Lions': 'Inter Lions', 'Dulwich Hill': 'Dulwich Hill',
    'Hakoah Sydney City East': 'Hakoah', 'Northern Tigers': 'Northern Tigers',
    'Sydney United 58': 'Sydney United', 'Bankstown City': 'Bankstown City',
    'Rydalmere Lions': 'Rydalmere', 'Canterbury Bankstown': 'Canterbury Bankstown',
    'Gladesville Ryde Magic': 'Gladesville', 'Hills United': 'Hills United',
    'Parramatta FC': 'Parramatta', 'Nepean FC': 'Nepean',
    'Hawkesbury City': 'Hawkesbury', 'Western Rage': 'Western Rage',
    'South Coast Flame': 'South Coast Flame', 'Bankstown United': 'Bankstown United',
    'Central Coast United': 'Central Coast United', 'Newcastle Olympic': 'Newcastle Olympic',
    'Charlestown Azzurri': 'Charlestown', 'Lambton Jaffas': 'Lambton Jaffas',
    'Edgeworth Eagles': 'Edgeworth', 'Broadmeadow Magic': 'Broadmeadow',
    'Maitland FC': 'Maitland', 'Weston Bears': 'Weston Bears',
    'Adamstown Rosebud': 'Adamstown', 'Valentine Phoenix': 'Valentine',
    'Lake Macquarie City': 'Lake Macquarie', 'Cooks Hill United': 'Cooks Hill',
    'New Lambton FC': 'New Lambton', 'Kotara South': 'Kotara South',
    'Wallsend FC': 'Wallsend', 'Hamilton Azzurri': 'Hamilton Azzurri',
    'Thornton Redbacks': 'Thornton', 'Singleton Strikers': 'Singleton',
    'Cardiff City': 'Cardiff City', 'Toronto Awaba Stags': 'Toronto Awaba',
    'Garden Suburb': 'Garden Suburb', 'Kahibah FC': 'Kahibah',
    'Raymond Terrace': 'Raymond Terrace', 'Barnsley': 'Barnsley',
}


# ========== Elo数据获取与先验计算 ==========
def fetch_clubelo_rankings():
    """从clubelo.com爬取Top 50球队的Elo评分（2000分制），失败时回退到本地缓存"""
    # 1. 优先在线获取
    try:
        url = "https://clubelo.com/Rankings"
        req = Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        with urlopen(req, timeout=10) as resp:
            html = resp.read().decode('utf-8', errors='ignore')

        # 从Vega-Lite图表JSON中按位置配对提取Name和Elo
        names = re.findall(r'"Name":\s*"([^"]+)"', html)
        elos = re.findall(r'"Elo":\s*([\d.]+)', html)

        elo_dict = {}
        n = min(len(names), len(elos))
        for i in range(n):
            try:
                name = names[i].encode('utf-8').decode('unicode_escape')
            except Exception:
                name = names[i]
            elo = float(elos[i])
            elo_dict[name] = elo

        if elo_dict:
            print(f"[Elo] 在线获取 {len(elo_dict)} 支球队的ClubElo评分")
            top5 = sorted(elo_dict.items(), key=lambda x: -x[1])[:5]
            print(f"[Elo] Top5: {', '.join(f'{n}({e:.0f})' for n, e in top5)}")
            # 更新本地缓存
            try:
                with open('elo_data.json', 'w', encoding='utf-8') as f:
                    json.dump(elo_dict, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            return elo_dict
    except Exception as e:
        print(f"[Elo] 在线获取ClubElo失败: {e}")

    # 2. 回退到本地缓存文件
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        local_path = os.path.join(script_dir, 'elo_data.json')
        if os.path.exists(local_path):
            with open(local_path, 'r', encoding='utf-8') as f:
                elo_dict = json.load(f)
            if elo_dict:
                print(f"[Elo] 使用本地缓存 {len(elo_dict)} 支球队的ClubElo评分")
                return elo_dict
    except Exception as e:
        print(f"[Elo] 读取本地缓存失败: {e}")

    print("[Elo] 无可用Elo数据，将使用联赛平均先验")
    return {}


def get_team_elo(team_name, elo_data):
    """通过球队名称映射获取Elo评分"""
    if not elo_data:
        return None
    # 直接匹配
    if team_name in elo_data:
        return elo_data[team_name]
    # 通过映射表匹配
    mapped = TEAM_NAME_MAP.get(team_name)
    if mapped and mapped in elo_data:
        return elo_data[mapped]
    # 模糊匹配（包含关系）
    team_lower = team_name.lower().replace(' ', '')
    for elo_name, elo_val in elo_data.items():
        if elo_name.lower().replace(' ', '') in team_lower or team_lower in elo_name.lower().replace(' ', ''):
            return elo_val
    return None


def elo_to_prior_football(home_elo, away_elo, league_draw_rate=0.26):
    """
    用Elo差值计算足球先验概率 [主胜, 平局, 客胜]
    基于ClubElo标准期望得分公式 + 平局率建模
    期望得分 = 主胜*1 + 平局*0.5 + 客胜*0
    """
    if home_elo is None or away_elo is None:
        return None

    # 主场优势约60 Elo分
    home_advantage = 60.0
    elo_diff = home_elo - away_elo + home_advantage

    # ClubElo期望得分公式（胜=1, 平=0.5, 负=0）
    expected_score = 1.0 / (1.0 + 10 ** (-elo_diff / 400.0))

    # 平局概率：实力越接近平局率越高，偏离时降低（最低30%基准）
    draw_factor = max(0.3, 1.0 - abs(elo_diff) / 800.0)
    p_draw = league_draw_rate * draw_factor

    # 由期望得分公式推导：expected_score = p_home + 0.5 * p_draw
    p_home = expected_score - 0.5 * p_draw
    p_away = 1.0 - p_home - p_draw

    # 边界保护（防止极端情况出现负值）
    p_home = max(0.01, min(0.98, p_home))
    p_draw = max(0.01, min(0.5, p_draw))
    p_away = max(0.01, min(0.98, p_away))

    # 归一化
    total = p_home + p_draw + p_away
    return [p_home / total, p_draw / total, p_away / total]


def elo_to_prior_basketball(home_elo, away_elo):
    """用Elo差值计算篮球先验概率 [主胜, 客胜]"""
    if home_elo is None or away_elo is None:
        return None
    # 篮球主场优势约100 Elo分（得分影响更大）
    home_advantage = 100.0
    elo_diff = home_elo - away_elo + home_advantage
    p_home = 1.0 / (1.0 + 10 ** (-elo_diff / 400.0))
    return [p_home, 1.0 - p_home]


def estimate_basketball_elo_from_odds(events):
    """
    篮球简易Elo系统：基于市场赔率反推球队相对实力
    用所有比赛的赔率中位数建立球队实力评分（1500分制）
    """
    team_strength = {}
    team_count = {}

    for event in events:
        home = event.get('home_team', '')
        away = event.get('away_team', '')
        bookmakers = event.get('bookmakers', [])
        if not bookmakers:
            continue

        odds_list = []
        for bk in bookmakers:
            for market in bk.get('markets', []):
                if market.get('key') == 'h2h':
                    outcomes = {o['name']: o['price'] for o in market.get('outcomes', [])}
                    h = outcomes.get(home, 0)
                    a = outcomes.get(away, 0)
                    if h > 1.0 and a > 1.0:
                        odds_list.append((h, a))
                    break

        if not odds_list:
            continue

        # 中位数赔率
        med_h = sorted(o[0] for o in odds_list)[len(odds_list) // 2]
        med_a = sorted(o[1] for o in odds_list)[len(odds_list) // 2]

        # 去水概率
        inv_h = 1.0 / med_h
        inv_a = 1.0 / med_a
        total_inv = inv_h + inv_a
        p_h = inv_h / total_inv
        p_a = inv_a / total_inv

        # 概率转Elo差值（主场优势100分）
        if p_h > 0 and p_a > 0:
            elo_diff = 400 * math.log10(p_h / p_a) - 100
            # 主客队各分配一半差值
            for team, delta in [(home, elo_diff / 2), (away, -elo_diff / 2)]:
                team_strength[team] = team_strength.get(team, 1500.0) + delta
                team_count[team] = team_count.get(team, 0) + 1

    # 归一化到1500基准
    if team_strength:
        avg = sum(team_strength.values()) / len(team_strength)
        for team in team_strength:
            team_strength[team] = 1500.0 + (team_strength[team] - avg)

    print(f"[Elo-篮球] 基于赔率估算了 {len(team_strength)} 支球队的简易Elo")
    return team_strength


# ========== 伤病信息接口 ==========
def fetch_injury_data():
    """
    伤病信息接入接口（占位实现）
    实际使用时可替换为付费API（如API-Football、Sportmonks等）
    返回格式: {球队名: [伤病球员列表]}
    """
    # 占位：无免费结构化伤病数据源
    # 如需接入，设置环境变量 INJURY_API_KEY 并在此处调用API
    injury_api_key = os.environ.get('INJURY_API_KEY', '')
    if not injury_api_key:
        return {}

    # 示例接入代码（需用户配置API后启用）：
    # try:
    #     url = f"https://api.example.com/injuries?api_key={injury_api_key}"
    #     resp = requests.get(url, timeout=10)
    #     data = resp.json()
    #     injury_dict = {}
    #     for item in data.get('response', []):
    #         team = item.get('team', {}).get('name', '')
    #         player = item.get('player', {}).get('name', '')
    #         if team and player:
    #             injury_dict.setdefault(team, []).append(player)
    #     return injury_dict
    # except Exception as e:
    #     print(f"[伤病] 获取失败: {e}")
    #     return {}
    return {}


def apply_injury_adjustment(home_elo, away_elo, home_team, away_team, injury_data):
    """根据伤病信息调整Elo评分（核心球员伤病约扣30-80分）"""
    if not injury_data:
        return home_elo, away_elo

    home_injuries = injury_data.get(home_team, [])
    away_injuries = injury_data.get(away_team, [])

    # 每支球队最多扣100分（防止过度调整）
    home_penalty = min(len(home_injuries) * 25, 100)
    away_penalty = min(len(away_injuries) * 25, 100)

    if home_elo is not None:
        home_elo = home_elo - home_penalty
    if away_elo is not None:
        away_elo = away_elo - away_penalty

    return home_elo, away_elo


# ========== 真正的贝叶斯分析 ==========
def bayesian_analysis(events, sport_type='football', elo_data=None, injury_data=None):
    is_basketball = sport_type == 'basketball'
    results = []

    for event in events:
        home = event.get('home_team', '')
        away = event.get('away_team', '')
        commence = event.get('commence_time', '')
        sport_key = event.get('sport_key', '')
        sport_title = event.get('sport_title', '')
        bookmakers = event.get('bookmakers', [])

        if not bookmakers:
            continue

        # 收集各公司赔率 (h2h胜平负 + totals进球数大小球)
        odds_by_company = {}
        totals_by_company = {}  # {公司名: {point: {over: odds, under: odds}}}
        for bk in bookmakers:
            bk_name = bk.get('title', bk.get('key', ''))
            for market in bk.get('markets', []):
                mkey = market.get('key')
                if mkey == 'h2h':
                    outcomes = {o['name']: o['price'] for o in market.get('outcomes', [])}
                    h = outcomes.get(home, 0)
                    a = outcomes.get(away, 0)
                    d = outcomes.get('Draw', 0) if not is_basketball else 0
                    if h > 1.0 and a > 1.0:
                        if not is_basketball and d <= 1.0:
                            continue
                        odds_by_company[bk_name] = {'home': h, 'draw': d, 'away': a}
                elif mkey == 'totals' and not is_basketball:
                    # 进球数大小球: outcomes包含Over/Under, point是盘口(如2.5)
                    outcomes = market.get('outcomes', [])
                    if len(outcomes) >= 2:
                        point = outcomes[0].get('point', 2.5)
                        over_odds = 0
                        under_odds = 0
                        for o in outcomes:
                            oname = o.get('name', '').lower()
                            if 'over' in oname:
                                over_odds = o.get('price', 0)
                            elif 'under' in oname:
                                under_odds = o.get('price', 0)
                        if over_odds > 1.0 and under_odds > 1.0:
                            totals_by_company[bk_name] = {
                                'point': point,
                                'over': over_odds,
                                'under': under_odds
                            }

        if len(odds_by_company) < 1:
            continue

        def median(lst):
            s = sorted(lst)
            n = len(s)
            if n % 2 == 0:
                return (s[n // 2 - 1] + s[n // 2]) / 2
            return s[n // 2]

        def std_dev(lst):
            if len(lst) < 2:
                return 0.0
            m = sum(lst) / len(lst)
            return math.sqrt(sum((x - m) ** 2 for x in lst) / len(lst))

        all_home = [o['home'] for o in odds_by_company.values()]
        all_away = [o['away'] for o in odds_by_company.values()]
        all_draw = [o['draw'] for o in odds_by_company.values() if o['draw'] > 0] if not is_basketball else []

        med_home = median(all_home)
        med_away = median(all_away)
        med_draw = median(all_draw) if all_draw else 0

        best_home = max(all_home)
        best_away = max(all_away)
        best_draw = max(all_draw) if all_draw else 0

        std_home = std_dev(all_home)
        std_away = std_dev(all_away)
        avg_std = (std_home + std_away) / 2

        # 获取球队Elo
        home_elo = get_team_elo(home, elo_data) if elo_data else None
        away_elo = get_team_elo(away, elo_data) if elo_data else None

        # ===== 进球数大小球 (Totals) 分析 =====
        totals_data = None
        if not is_basketball and totals_by_company:
            # 取最常见的盘口
            from collections import Counter
            point_counts = Counter(t['point'] for t in totals_by_company.values())
            main_point = point_counts.most_common(1)[0][0] if point_counts else 2.5

            # 收集该盘口下的所有赔率
            over_odds_list = []
            under_odds_list = []
            for t in totals_by_company.values():
                if abs(t['point'] - main_point) < 0.01:
                    over_odds_list.append(t['over'])
                    under_odds_list.append(t['under'])

            if over_odds_list and under_odds_list:
                med_over = median(over_odds_list)
                med_under = median(under_odds_list)
                best_over = max(over_odds_list)
                best_under = max(under_odds_list)

                # 去水概率
                inv_over = 1.0 / med_over
                inv_under = 1.0 / med_under
                total_inv = inv_over + inv_under
                p_over = inv_over / total_inv
                p_under = inv_under / total_inv

                totals_data = {
                    'point': main_point,
                    'med_over': round(med_over, 2),
                    'med_under': round(med_under, 2),
                    'best_over': round(best_over, 2),
                    'best_under': round(best_under, 2),
                    'p_over': round(p_over, 4),
                    'p_under': round(p_under, 4),
                    'num_companies': len(over_odds_list),
                    'companies': {
                        n: {'over': round(t['over'], 2), 'under': round(t['under'], 2), 'point': t['point']}
                        for n, t in list(totals_by_company.items())[:8]
                    }
                }

        # ===== 角球数量预估模型 =====
        corner_prediction = None
        if not is_basketball:
            # 联赛平均角球数 (基于联赛特性)
            league_corner_avg = {
                'soccer_epl': 10.5, 'soccer_spain_la_liga': 10.0,
                'soccer_germany_bundesliga': 11.0, 'soccer_italy_serie_a': 9.5,
                'soccer_france_ligue_one': 10.0, 'soccer_uefa_champs_league': 10.5,
                'soccer_uefa_europa_league': 10.0, 'soccer_netherlands_eredivisie': 10.5,
                'soccer_portugal_primeira_liga': 10.0, 'soccer_usa_mls': 10.5,
                'soccer_brazil_campeonato_serie_a': 11.0, 'soccer_mexico_liga_mx': 10.0,
            }.get(sport_key, 10.0)

            # 基于Elo差值和进攻强度调整角球预估
            # 强队进攻角球更多，比赛开放程度影响角球总数
            corner_base = league_corner_avg

            if home_elo is not None and away_elo is not None:
                elo_diff = abs(home_elo - away_elo)
                # 实力差距大的比赛角球更多（强队围攻）
                corner_base += min(elo_diff / 200.0, 1.5)
                # 双方Elo都高的比赛更开放，角球略多
                avg_elo = (home_elo + away_elo) / 2
                if avg_elo > 1900:
                    corner_base += 0.5
                elif avg_elo < 1700:
                    corner_base -= 0.5

            # 泊松lambda用于角球分布
            corner_lambda = max(6.0, min(16.0, corner_base))

            # 计算角球大小球概率 (盘口9.5)
            corner_line = 9.5
            p_corner_over = 0.0
            p_corner_under = 0.0
            for k in range(30):
                pk = math.exp(-corner_lambda) * (corner_lambda ** k) / math.factorial(k)
                if k > corner_line:
                    p_corner_over += pk
                else:
                    p_corner_under += pk

            # 最可能角球数
            most_likely_corners = int(corner_lambda)
            corner_prob_at_mode = math.exp(-corner_lambda) * (corner_lambda ** most_likely_corners) / math.factorial(most_likely_corners)

            corner_prediction = {
                'expected': round(corner_lambda, 1),
                'most_likely': most_likely_corners,
                'most_likely_prob': round(corner_prob_at_mode, 4),
                'line': corner_line,
                'p_over': round(p_corner_over, 4),
                'p_under': round(p_corner_under, 4),
                'league_avg': league_corner_avg,
                'distribution': [
                    {'corners': k, 'prob': round(math.exp(-corner_lambda) * (corner_lambda ** k) / math.factorial(k), 4)}
                    for k in range(0, 16)
                ]
            }

        # 联赛先验（作为Elo不可用时的回退）
        prior_data = LEAGUE_PRIORS.get(sport_key, DEFAULT_BASKETBALL_PRIOR if is_basketball else DEFAULT_FOOTBALL_PRIOR)
        league_draw_rate = prior_data.get('draw', 0.26) if not is_basketball else 0

        # 优先使用Elo先验，回退到联赛平均先验
        elo_prior_used = False
        if is_basketball:
            elo_prior = elo_to_prior_basketball(home_elo, away_elo)
            if elo_prior is not None:
                prior = elo_prior
                elo_prior_used = True
            else:
                prior = [prior_data['home'], prior_data['away']]
        else:
            elo_prior = elo_to_prior_football(home_elo, away_elo, league_draw_rate)
            if elo_prior is not None:
                prior = elo_prior
                elo_prior_used = True
            else:
                prior = [prior_data['home'], prior_data['draw'], prior_data['away']]

        # Elo先验强度更高（球队级别数据比联赛平均更可靠）
        effective_prior_strength = PRIOR_STRENGTH * 2.0 if elo_prior_used else PRIOR_STRENGTH

        # 似然: 中位数赔率去水后的市场概率
        if is_basketball:
            market_probs = shin_dewater([med_home, med_away])
        else:
            market_probs = shin_dewater([med_home, med_draw, med_away])

        # 贝叶斯后验 (Dirichlet-Multinomial共轭)
        likelihood_strength = 10.0 / (1.0 + avg_std * 20)
        total_strength = effective_prior_strength + likelihood_strength

        posterior_raw = [
            (prior[i] * effective_prior_strength + market_probs[i] * likelihood_strength) / total_strength
            for i in range(len(prior))
        ]
        total_p = sum(posterior_raw)
        posterior = [p / total_p for p in posterior_raw]

        if is_basketball:
            post_home, post_away = posterior[0], posterior[1]
            post_draw = 0.0
            p_market_home, p_market_away = market_probs[0], market_probs[1]
            p_market_draw = 0.0
        else:
            post_home, post_draw, post_away = posterior[0], posterior[1], posterior[2]
            p_market_home, p_market_draw, p_market_away = market_probs[0], market_probs[1], market_probs[2]

        # Edge = 后验概率 - 1/最高赔率
        edge_home = (post_home - 1.0 / best_home) * 100
        edge_away = (post_away - 1.0 / best_away) * 100
        if not is_basketball:
            edge_draw = (post_draw - 1.0 / best_draw) * 100 if best_draw > 0 else 0
        else:
            edge_draw = 0.0

        # 凯利指数 (用后验概率 + 最高可获得赔率)
        k_home = kelly_fraction(post_home, best_home)
        k_away = kelly_fraction(post_away, best_away)
        k_draw = kelly_fraction(post_draw, best_draw) if not is_basketball and best_draw > 0 else 0.0

        kelly_ratio_home = round(post_home * best_home, 3)
        kelly_ratio_away = round(post_away * best_away, 3)
        kelly_ratio_draw = round(post_draw * best_draw, 3) if not is_basketball and best_draw > 0 else 0.0

        # 泊松比分预测 (仅足球) - Elo驱动的lambda调整
        poisson_scores = []
        if not is_basketball:
            base_lam_h = prior_data.get('lam_h', 1.5)
            base_lam_a = prior_data.get('lam_a', 1.2)

            if home_elo is not None and away_elo is not None:
                # Elo差值调整lambda（更准确的进球期望）
                elo_diff = home_elo - away_elo + 60  # 主场优势60分
                # 每100分Elo差约对应0.15球的期望差
                goal_diff = elo_diff / 100.0 * 0.15
                # 限制调整幅度在±50%
                adj_h = max(0.5, min(2.0, 1.0 + goal_diff / base_lam_h))
                adj_a = max(0.5, min(2.0, 1.0 - goal_diff / base_lam_a))
                lam_h = base_lam_h * adj_h
                lam_a = base_lam_a * adj_a
            else:
                # 回退：用后验概率强度调整
                strength_h = post_home / 0.45
                strength_a = post_away / 0.29
                lam_h = base_lam_h * (0.7 + 0.6 * strength_h)
                lam_a = base_lam_a * (0.7 + 0.6 * strength_a)

            poisson_scores = poisson_predict(lam_h, lam_a)

        best_score = poisson_scores[0] if poisson_scores else {'home': 0, 'away': 0, 'prob': 0}
        top5 = poisson_scores[:5]
        matrix = poisson_scores[:36]

        result = {
            'match': f'{home} vs {away}',
            'home': home,
            'away': away,
            'commence': commence,
            'sport': sport_type,
            'league': translate_league_name(sport_title),
            'league_en': sport_title,
            'sport_key': sport_key,
            'odds': {
                'home': round(med_home, 2),
                'draw': round(med_draw, 2) if not is_basketball else 0,
                'away': round(med_away, 2),
                'best_home': round(best_home, 2),
                'best_draw': round(best_draw, 2) if not is_basketball else 0,
                'best_away': round(best_away, 2),
            },
            'companies': {
                n: {k: round(v, 2) for k, v in odds.items()}
                for n, odds in list(odds_by_company.items())[:8]
            },
            'market': {
                'home': round(p_market_home, 4),
                'draw': round(p_market_draw, 4),
                'away': round(p_market_away, 4),
            },
            'bayes': {
                'home': round(post_home, 4),
                'draw': round(post_draw, 4),
                'away': round(post_away, 4),
            },
            'elo': {
                'home': round(home_elo, 0) if home_elo is not None else None,
                'away': round(away_elo, 0) if away_elo is not None else None,
                'used': elo_prior_used,
            },
            'edge': {
                'home': round(edge_home, 2),
                'draw': round(edge_draw, 2),
                'away': round(edge_away, 2),
            },
            'kelly': {
                'home': k_home,
                'draw': k_draw,
                'away': k_away,
            },
            'kelly_ratio': {
                'home': kelly_ratio_home,
                'draw': kelly_ratio_draw,
                'away': kelly_ratio_away,
            },
            'poisson': {
                'score': f"{best_score['home']}-{best_score['away']}" if poisson_scores else '',
                'prob': round(best_score['prob'], 4) if poisson_scores else 0,
                'top5': top5,
                'matrix': matrix,
            },
            'dispersion': {
                'std_home': round(std_home, 4),
                'std_away': round(std_away, 4),
                'avg_std': round(avg_std, 4),
                'num_companies': len(odds_by_company),
            },
            'totals': totals_data,
            'corners': corner_prediction,
            'status': 'upcoming',
            'confidence': 'high' if avg_std < 0.05 else 'medium' if avg_std < 0.1 else 'low',
        }
        results.append(result)

    return results


# ========== HTML 模板 ==========
def get_html_template():
    return r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>赛事赔率贝叶斯分析看板</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<script>if(typeof echarts==='undefined'){document.write('<scr'+'ipt src="https://cdnjs.cloudflare.com/ajax/libs/echarts/5.5.0/echarts.min.js"><\/scr'+'ipt>')}</script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:Tahoma,"Microsoft YaHei",sans-serif;background:#004b81;color:#333;font-size:12px}
a{color:#333;text-decoration:none}a:hover{color:#e62129}
#tools{width:100%;max-width:1200px;margin:0 auto;padding:6px 10px;background:#f6f6f6;border-bottom:1px solid #c0c0c0;display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:12px}
#tools .btn{display:inline-block;padding:2px 10px;line-height:22px;border:1px solid #c0c0c0;border-radius:2px;background:#fff;color:#333;cursor:pointer;font-size:12px}
#tools .btn:hover{border-color:#93c1d8;color:#228bd6}
#tools .btn.on{background:#FFEEB9;border-color:#DEA67C}
#tools .txt{color:#666;padding:0 4px}
#tools .info{margin-left:auto;color:#888;font-size:11px}
.page{max-width:1200px;margin:0 auto;background:#fff;min-height:100vh;box-shadow:0 0 20px rgba(0,0,0,.3)}
.hdr{background:linear-gradient(180deg,#1a6db5,#0d5a9e);padding:10px 16px;color:#fff;display:flex;justify-content:space-between;align-items:center;border-bottom:2px solid #004080}
.hdr h1{font-size:15px;font-weight:bold}
.hdr .tm{font-size:11px;color:#b8d4f0}
.kpi{display:grid;grid-template-columns:repeat(5,1fr);border-bottom:1px solid #d0d0d0}
.kpi-cell{text-align:center;padding:12px 8px;border-right:1px solid #e0e0e0;background:#f8fafc}
.kpi-cell:last-child{border-right:none}
.kpi-cell .v{font-size:24px;font-weight:bold;font-family:Tahoma,Arial}
.kpi-cell .v.c1{color:#1a6db5}.kpi-cell .v.c2{color:#2e7d32}.kpi-cell .v.c3{color:#e65100}.kpi-cell .v.c4{color:#c62828}.kpi-cell .v.c5{color:#7b1fa2}
.kpi-cell .l{font-size:11px;color:#888;margin-top:3px}
.sec-hdr{background:linear-gradient(180deg,#e8f0f8,#d0dfe8);border-top:1px solid #b0c4d8;border-bottom:1px solid #b0c4d8;padding:7px 14px;font-size:13px;font-weight:bold;color:#1a4a7a}
.tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
.mtbl{width:100%;border-collapse:collapse;font-size:12px;min-width:1000px}
.mtbl thead th{background:#e0ecf5;padding:6px 8px;text-align:center;font-weight:bold;color:#1a4a7a;border-bottom:2px solid #1a6db5;font-size:11px;white-space:nowrap;position:sticky;top:0;z-index:1}
.mtbl tbody tr{border-bottom:1px solid #e8e8e8}
.mtbl tbody tr:hover{background:#f0f6ff}
.mtbl tbody tr:nth-child(even){background:#fafbfc}
.mtbl td{padding:7px 8px;text-align:center;vertical-align:middle;white-space:nowrap}
.mtbl .lg{display:inline-block;padding:1px 6px;border-radius:2px;font-size:10px;font-weight:bold;color:#fff}
.mtbl .lg.fb{background:#1B5E20}.mtbl .lg.bb{background:#E65100}
.mtbl .tn{font-weight:bold;color:#333}
.mtbl .th{text-align:right;padding-right:4px}.mtbl .ta{text-align:left;padding-left:4px}
.mtbl .od{font-family:Tahoma,Arial;font-weight:bold;font-size:12px}
.mtbl .od.best{color:#2e7d32;text-decoration:underline}
.mtbl .vs{color:#999;font-weight:normal;font-size:11px}
.mtbl .pbar{display:inline-flex;height:10px;border-radius:2px;overflow:hidden;vertical-align:middle;margin-right:4px}
.mtbl .pbar .h{background:#2e7d32}.mtbl .pbar .d{background:#f5a623}.mtbl .pbar .a{background:#c62828}
.mtbl .pt{font-family:Tahoma,Arial;font-size:11px;font-weight:bold}
.mtbl .pt-h{color:#2e7d32}.mtbl .pt-m{color:#e65100}.mtbl .pt-l{color:#666}
.mtbl .vb{display:inline-block;padding:1px 5px;border-radius:2px;font-size:10px;font-weight:bold}
.mtbl .vb.pos{background:#e8f5e9;color:#2e7d32}.mtbl .vb.neg{background:#ffebee;color:#c62828}
.mtbl .kb{display:inline-block;padding:1px 4px;border-radius:2px;font-size:10px;font-family:Tahoma;font-weight:bold}
.mtbl .kb.safe{background:#e8f5e9;color:#2e7d32}.mtbl .kb.warn{background:#fff3e0;color:#e65100}.mtbl .kb.danger{background:#ffebee;color:#c62828}.mtbl .kb.zero{background:#f5f5f5;color:#999}
.vsec{padding:10px 14px;border-bottom:1px solid #e0e0e0}
.vlist{display:flex;flex-wrap:wrap;gap:6px}
.vitem{display:flex;align-items:center;gap:6px;padding:5px 10px;background:#f0f9f0;border:1px solid #c8e6c9;border-radius:3px;font-size:12px}
.vitem .nm{font-weight:bold}.vitem .eg{font-family:Tahoma;font-weight:bold;color:#2e7d32;font-size:13px}
.vitem .dt{font-size:10px;color:#888;font-family:Tahoma}
.chsec{padding:14px;border-bottom:1px solid #e0e0e0}
.chrow{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.chbox{background:#f8fafc;border:1px solid #d0dfe8;border-radius:4px;padding:10px}
.chbox .cl{font-size:12px;font-weight:bold;color:#1a4a7a;margin-bottom:6px;display:flex;align-items:center;gap:4px}
.chbox .cl .dot{width:7px;height:7px;border-radius:50%}
.chbox .cc{width:100%;height:280px}
.ftr{background:#f6f6f6;padding:10px 14px;font-size:11px;color:#888;line-height:1.7;border-top:1px solid #e0e0e0}
.ftr b{color:#1a6db5}
.err-msg{text-align:center;padding:30px;color:#c62828;font-size:14px;background:#fff3e0;border:1px solid #ffcc80;border-radius:4px;margin:10px}
@media(max-width:768px){
  .kpi{grid-template-columns:repeat(3,1fr)}
  .chrow{grid-template-columns:1fr}
  .chbox .cc{height:220px}
  .mtbl{font-size:11px;min-width:800px}
  .mtbl td{padding:5px 4px}
  .hdr h1{font-size:13px}
  #tools{font-size:11px}
  .vlist{flex-direction:column}
}
/* ===== 比赛详情弹窗 ===== */
.modal-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.6);z-index:1000;display:none;justify-content:center;align-items:flex-start;overflow-y:auto;padding:20px}
.modal-overlay.show{display:flex}
.modal{background:#fff;border-radius:6px;max-width:900px;width:100%;margin-top:20px;box-shadow:0 10px 40px rgba(0,0,0,0.3)}
.modal-header{background:linear-gradient(135deg,#004b81,#0066aa);color:#fff;padding:16px 20px;border-radius:6px 6px 0 0;display:flex;justify-content:space-between;align-items:center}
.modal-header h3{font-size:16px;margin:0}
.modal-header .league-tag{background:rgba(255,255,255,0.2);padding:2px 8px;border-radius:3px;font-size:11px;margin-left:8px}
.modal-close{background:none;border:none;color:#fff;font-size:22px;cursor:pointer;padding:0 6px;line-height:1}
.modal-close:hover{color:#ffcc00}
.modal-body{padding:20px}
.modal-section{margin-bottom:20px}
.modal-section h4{font-size:13px;color:#004b81;border-bottom:2px solid #004b81;padding-bottom:4px;margin-bottom:10px}
.detail-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.detail-card{background:#f8f9fa;border:1px solid #e9ecef;border-radius:4px;padding:12px}
.detail-card .label{font-size:11px;color:#666;margin-bottom:4px}
.detail-card .value{font-size:18px;font-weight:bold;color:#333}
.detail-card .value.green{color:#28a745}
.detail-card .value.red{color:#dc3545}
.detail-card .value.orange{color:#fd7e14}
.odds-table{width:100%;border-collapse:collapse;font-size:12px}
.odds-table th{background:#004b81;color:#fff;padding:6px 8px;text-align:left;font-weight:normal}
.odds-table td{padding:6px 8px;border-bottom:1px solid #eee}
.odds-table tr:hover{background:#f5f5f5}
.odds-table .best{color:#28a745;font-weight:bold}
.prob-bar{height:20px;background:#e9ecef;border-radius:3px;overflow:hidden;display:flex;margin:4px 0}
.prob-bar .h{background:#004b81}
.prob-bar .d{background:#6c757d}
.prob-bar .a{background:#dc3545}
.prob-bar span{display:flex;align-items:center;justify-content:center;color:#fff;font-size:10px;min-width:30px}
.corner-dist{display:flex;align-items:flex-end;gap:2px;height:80px;padding:8px 0}
.corner-bar{flex:1;background:linear-gradient(180deg,#17a2b8,#007bff);border-radius:2px 2px 0 0;position:relative;min-width:8px}
.corner-bar .cval{position:absolute;top:-16px;left:50%;transform:translateX(-50%);font-size:9px;color:#666}
.corner-bar .clabel{position:absolute;bottom:-16px;left:50%;transform:translateX(-50%);font-size:9px;color:#999}
.poisson-matrix{display:grid;grid-template-columns:repeat(6,1fr);gap:2px;font-size:11px}
.poisson-cell{background:#f8f9fa;padding:6px;text-align:center;border-radius:2px}
.poisson-cell.highlight{background:#fff3cd;font-weight:bold}
.match-teams{display:flex;justify-content:space-between;align-items:center;padding:12px 0;border-bottom:1px solid #eee;margin-bottom:12px}
.match-team{text-align:center;flex:1}
.match-team .name{font-size:15px;font-weight:bold;color:#333}
.match-team .elo{font-size:11px;color:#666;margin-top:2px}
.match-vs{font-size:20px;color:#999;padding:0 16px}
</style>
</head>
<body>
<div class="page">
  <div id="tools">
    <a class="btn on" onclick="showAll()">全部赛事</a>
    <a class="btn" onclick="showFB()">⚽ 足球</a>
    <a class="btn" onclick="showBB()">🏀 篮球</a>
    <span class="txt">|</span>
    <a class="btn" onclick="showHot()">🔥 热门</a>
    <a class="btn" onclick="showVal()">💰 价值投注</a>
    <span class="info" id="toolsInfo">数据源: The Odds API · 每30分钟自动更新</span>
  </div>
  <div class="hdr">
    <h1>📊 赛事赔率 · 贝叶斯分析看板</h1>
    <div class="tm" id="updateTime">加载中...</div>
  </div>
  <div class="kpi">
    <div class="kpi-cell"><div class="v c1" id="kTotal">—</div><div class="l">赛事总数</div></div>
    <div class="kpi-cell"><div class="v c2" id="kFB">—</div><div class="l">足球赛事</div></div>
    <div class="kpi-cell"><div class="v c3" id="kBB">—</div><div class="l">篮球赛事</div></div>
    <div class="kpi-cell"><div class="v c4" id="kVal">—</div><div class="l">价值投注 (Edge&gt;2%)</div></div>
    <div class="kpi-cell"><div class="v c5" id="kEV">—</div><div class="l">正EV机会</div></div>
  </div>
  <div class="sec-hdr">📋 赛事列表 — 贝叶斯后验概率 &amp; 赔率分析 (最高赔率标绿)</div>
  <div class="tbl-wrap">
    <table class="mtbl"><thead><tr>
      <th>赛事</th><th>开赛(北京)</th><th>主队</th><th></th><th>客队</th>
      <th>主胜</th><th>平局</th><th>客胜</th>
      <th>贝叶斯后验</th><th>泊松Top3</th><th>凯利f*</th><th>Edge</th>
    </tr></thead><tbody id="tbody"></tbody></table>
  </div>
  <div id="errMsg"></div>
  <div class="sec-hdr">💰 价值投注雷达 — 正EV机会 (Edge &gt; 2%)</div>
  <div class="vsec"><div class="vlist" id="vlist"></div></div>
  <div class="sec-hdr">📈 数据分析图表</div>
  <div class="chsec"><div class="chrow">
    <div class="chbox"><div class="cl"><span class="dot" style="background:#1a6db5"></span>贝叶斯后验 vs 市场概率</div><div id="c1" class="cc"></div></div>
    <div class="chbox"><div class="cl"><span class="dot" style="background:#e65100"></span>多公司赔率离散度</div><div id="c2" class="cc"></div></div>
  </div></div>
  <div class="chsec"><div class="chrow">
    <div class="chbox"><div class="cl"><span class="dot" style="background:#2e7d32"></span>Edge 价值优势排序</div><div id="c3" class="cc"></div></div>
    <div class="chbox"><div class="cl"><span class="dot" style="background:#7b1fa2"></span>凯利f* 仓位建议</div><div id="c4" class="cc"></div></div>
  </div></div>
  <div class="chsec"><div class="chrow">
    <div class="chbox"><div class="cl"><span class="dot" style="background:#00695c"></span>泊松比分热力图</div><div id="c5" class="cc"></div></div>
    <div class="chbox"><div class="cl"><span class="dot" style="background:#c62828"></span>凯利比值风控 (&gt;1=正EV)</div><div id="c6" class="cc"></div></div>
  </div></div>
  <div class="ftr">
    <b>方法论 v4：</b>联赛先验(Dirichlet) + 市场赔率似然 → 贝叶斯后验概率 → Edge = 后验 - 1/最高赔率 → 凯利公式 f*=(pb-q)/b<br />
    <b>数据来源：</b>The Odds API（Bet365 / Pinnacle / William Hill / DraftKings 等 20+ 博彩公司）· 每30分钟 GitHub Actions 自动刷新<br />
    <b>时间说明：</b>开赛时间已转换为北京时间 (UTC+8) · 最高赔率标绿下划线
  </div>
</div>
<script>
var D=__DATA_PLACEHOLDER__;
var MD=D.football.concat(D.basketball);
var VB=[];
var mob=window.innerWidth<=768;

var now=Date.now();
var cutoff=now+7*24*3600*1000;
MD=MD.filter(function(m){
  if(!m.commence)return true;
  var t=new Date(m.commence).getTime();
  return t>now-3600000 && t<cutoff;
});

if(MD.length===0){
  document.getElementById('errMsg').innerHTML='<div class="err-msg">⚠️ 当前无可用赛事数据。可能原因：API 限流 / 密钥过期 / 暂无即将开赛的比赛。请稍后刷新或检查 GitHub Actions 运行日志。</div>';
}

MD.forEach(function(m){
  var edges=m.edge;
  if(edges.home>2)VB.push({n:m.home+' 主胜',e:edges.home,p:m.bayes.home,i:m.market.home,odds:m.odds.best_home,match:m.match,sport:m.sport});
  if(m.sport==='football'){
    if(edges.draw>2&&m.odds.best_draw>0)VB.push({n:m.home+' 平局',e:edges.draw,p:m.bayes.draw,i:m.market.draw,odds:m.odds.best_draw,match:m.match,sport:m.sport});
  }
  if(edges.away>2)VB.push({n:m.away+' 客胜',e:edges.away,p:m.bayes.away,i:m.market.away,odds:m.odds.best_away,match:m.match,sport:m.sport});
});
VB.sort(function(a,b){return b.e-a.e});

var evCount=0;
MD.forEach(function(m){
  if(m.kelly_ratio.home>1)evCount++;
  if(m.kelly_ratio.away>1)evCount++;
  if(m.sport==='football'&&m.kelly_ratio.draw>1)evCount++;
});

document.getElementById('updateTime').textContent='更新: '+(D.update_time||'')+' · 足球 '+D.football.length+' / 篮球 '+D.basketball.length;
document.getElementById('kTotal').textContent=MD.length;
document.getElementById('kFB').textContent=MD.filter(function(m){return m.sport==='football'}).length;
document.getElementById('kBB').textContent=MD.filter(function(m){return m.sport==='basketball'}).length;
document.getElementById('kVal').textContent=VB.length;
document.getElementById('kEV').textContent=evCount;

function toBJT(isoStr){
  if(!isoStr)return '—';
  var d=new Date(isoStr);
  try{
    return d.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
  }catch(e){
    var bj=new Date(d.getTime()+8*3600*1000);
    return (bj.getUTCMonth()+1)+'/'+bj.getUTCDate()+' '+String(bj.getUTCHours()).padStart(2,'0')+':'+String(bj.getUTCMinutes()).padStart(2,'0');
  }
}

function renderTable(data){
  var tb=document.getElementById('tbody');tb.innerHTML='';
  data.slice(0,40).forEach(function(m){
    var bb=m.sport==='basketball',lg=bb?'bb':'fb',lt=m.league||'';
    var ts=toBJT(m.commence);

    var bh='';
    if(bb){
      bh='<span class="pt pt-'+(m.bayes.home>.5?'h':'m')+'">主'+(m.bayes.home*100).toFixed(1)+'%</span>'
        +' / <span class="pt pt-'+(m.bayes.away>.5?'h':'m')+'">客'+(m.bayes.away*100).toFixed(1)+'%</span>';
    }else{
      bh='<span class="pt pt-'+(m.bayes.home>.5?'h':'m')+'">'+(m.bayes.home*100).toFixed(1)+'%</span>'
        +'<span class="pt" style="color:#999"> / '+(m.bayes.draw*100).toFixed(1)+'% / </span>'
        +'<span class="pt pt-'+(m.bayes.away>.5?'h':'m')+'">'+(m.bayes.away*100).toFixed(1)+'%</span>';
    }

    var bw=110,wh=Math.round(m.bayes.home*bw),wd=bb?0:Math.round(m.bayes.draw*bw),wa=bw-wh-wd;
    var pb='<div class="pbar" style="width:'+bw+'px"><div class="h" style="width:'+wh+'px"></div>'
      +(bb?'':'<div class="d" style="width:'+wd+'px"></div>')
      +'<div class="a" style="width:'+wa+'px"></div></div>';

    var ps='';
    if(m.poisson.top5&&m.poisson.top5.length>0){
      ps=m.poisson.top5.slice(0,3).map(function(s){
        return s.home+'-'+s.away+'('+(s.prob*100).toFixed(1)+'%)';
      }).join(' ');
    }else if(bb){
      ps='<span style="color:#999">—</span>';
    }

    var kf=m.kelly;
    function kcls(v){return v>0.05?'danger':v>0.02?'warn':v>0?'safe':'zero'}
    var kh='';
    if(bb){
      kh='<span class="kb '+kcls(kf.home)+'">'+kf.home.toFixed(3)+'</span> / '
        +'<span class="kb '+kcls(kf.away)+'">'+kf.away.toFixed(3)+'</span>';
    }else{
      kh='<span class="kb '+kcls(kf.home)+'">'+kf.home.toFixed(3)+'</span> '
        +'<span class="kb '+kcls(kf.draw)+'">'+kf.draw.toFixed(3)+'</span> '
        +'<span class="kb '+kcls(kf.away)+'">'+kf.away.toFixed(3)+'</span>';
    }

    var edges=m.edge;
    var validEdges=bb?[edges.home,edges.away]:[edges.home,edges.draw,edges.away];
    var maxE=Math.max.apply(null,validEdges);
    var minE=Math.min.apply(null,validEdges);
    var vl='';
    if(maxE>2){
      vl='<span class="vb pos">+'+maxE.toFixed(1)+'%</span>';
    }else if(maxE>0){
      vl='<span style="color:#666;font-size:10px">+'+maxE.toFixed(1)+'%</span>';
    }else{
      vl='<span class="vb neg">'+minE.toFixed(1)+'%</span>';
    }

    var oh='<span class="od'+(m.odds.home===m.odds.best_home?' best':'')+'">'+m.odds.home.toFixed(2)+'</span>';
    var oa='<span class="od'+(m.odds.away===m.odds.best_away?' best':'')+'">'+m.odds.away.toFixed(2)+'</span>';
    var od=bb?'<span style="color:#ccc">—</span>':'<span class="od'+(m.odds.draw===m.odds.best_draw?' best':'')+'">'+m.odds.draw.toFixed(2)+'</span>';

    var tr=document.createElement('tr');
    tr.style.cursor='pointer';
    tr.onmouseover=function(){this.style.background='#e8f4fc'};
    tr.onmouseout=function(){this.style.background=''};
    tr.onclick=function(){showMatchDetail(m)};
    tr.innerHTML='<td><span class="lg '+lg+'">'+lt+'</span></td>'
      +'<td style="font-family:Tahoma;font-size:11px;color:#666">'+ts+'</td>'
      +'<td class="tn th">'+m.home+'</td><td class="vs">vs</td><td class="tn ta">'+m.away+'</td>'
      +'<td>'+oh+'</td><td>'+od+'</td><td>'+oa+'</td>'
      +'<td>'+pb+'<br />'+bh+'</td>'
      +'<td style="font-size:10px;color:#7b1fa2;font-family:Tahoma">'+ps+'</td>'
      +'<td style="font-size:11px">'+kh+'</td>'
      +'<td>'+vl+'</td>';
    tb.appendChild(tr);
  });
}

function renderVB(){
  var el=document.getElementById('vlist');el.innerHTML='';
  if(!VB.length){el.innerHTML='<div style="color:#999;padding:8px">当前无 Edge&gt;2% 的价值投注机会。市场定价效率较高，或各博彩公司赔率差异较小。可降低阈值至1%查看更多机会。</div>';return}
  VB.slice(0,20).forEach(function(v){
    var d=document.createElement('div');d.className='vitem';
    d.innerHTML='<span class="nm">'+v.n+'</span><span class="eg">+'+v.e.toFixed(1)+'%</span><span class="dt">贝叶斯='+(v.p*100).toFixed(1)+'% / 赔率='+v.odds+' / EV='+((v.p*v.odds-1)*100).toFixed(1)+'%</span>';
    el.appendChild(d);
  });
}

function showAll(){renderTable(MD);hlBtn(0)}
function showFB(){renderTable(MD.filter(function(m){return m.sport==='football'}));hlBtn(1)}
function showBB(){renderTable(MD.filter(function(m){return m.sport==='basketball'}));hlBtn(2)}
function showHot(){renderTable(MD.slice(0,10));hlBtn(3)}
function showVal(){
  var v=[];MD.forEach(function(m){
    var validEdges=m.sport==='basketball'?[m.edge.home,m.edge.away]:[m.edge.home,m.edge.draw,m.edge.away];
    if(Math.max.apply(null,validEdges)>2)v.push(m);
  });
  renderTable(v);hlBtn(4);
}
function hlBtn(i){var bs=document.querySelectorAll('#tools .btn');bs.forEach(function(b,j){b.className=j===i?'btn on':'btn'})}

renderTable(MD);renderVB();

// ===== 比赛详情弹窗 =====
function closeModal(){document.getElementById('matchModal').classList.remove('show')}
function showMatchDetail(m){
  var bb=m.sport==='basketball';
  document.getElementById('modalTitle').textContent=m.home+' vs '+m.away;
  document.getElementById('modalLeague').textContent=m.league||m.league_en||'';
  document.getElementById('modalTime').textContent='开赛: '+toBJT(m.commence);

  var html='';
  // 球队信息 + Elo
  html+='<div class="match-teams">';
  html+='<div class="match-team"><div class="name">'+m.home+'</div>';
  if(m.elo&&m.elo.home)html+='<div class="elo">Elo: '+m.elo.home+'</div>';
  html+='</div>';
  html+='<div class="match-vs">VS</div>';
  html+='<div class="match-team"><div class="name">'+m.away+'</div>';
  if(m.elo&&m.elo.away)html+='<div class="elo">Elo: '+m.elo.away+'</div>';
  html+='</div></div>';

  // 贝叶斯后验概率
  html+='<div class="modal-section"><h4>贝叶斯后验概率</h4>';
  var bw=300,wh=Math.round(m.bayes.home*bw),wd=bb?0:Math.round(m.bayes.draw*bw),wa=bw-wh-wd;
  html+='<div class="prob-bar" style="width:'+bw+'px">';
  html+='<span class="h" style="width:'+wh+'px">主'+(m.bayes.home*100).toFixed(1)+'%</span>';
  if(!bb)html+='<span class="d" style="width:'+wd+'px">平'+(m.bayes.draw*100).toFixed(1)+'%</span>';
  html+='<span class="a" style="width:'+wa+'px">客'+(m.bayes.away*100).toFixed(1)+'%</span>';
  html+='</div>';
  if(m.elo&&m.elo.used)html+='<div style="font-size:11px;color:#28a745;margin-top:4px">✓ Elo先验已生效</div>';
  html+='</div>';

  // 胜平负指数 - 各公司对比
  html+='<div class="modal-section"><h4>胜平负指数（各博彩公司）</h4>';
  html+='<table class="odds-table"><thead><tr><th>博彩公司</th><th>主胜</th><th>'+(bb?'':'平局')+'</th><th>客胜</th></tr></thead><tbody>';
  var comps=m.companies||{};
  Object.keys(comps).forEach(function(cn){
    var o=comps[cn];
    html+='<tr><td>'+cn+'</td>';
    html+='<td class="'+(o.home===m.odds.best_home?'best':'')+'">'+o.home.toFixed(2)+'</td>';
    if(!bb)html+='<td class="'+(o.draw===m.odds.best_draw?'best':'')+'">'+(o.draw>0?o.draw.toFixed(2):'—')+'</td>';
    html+='<td class="'+(o.away===m.odds.best_away?'best':'')+'">'+o.away.toFixed(2)+'</td></tr>';
  });
  html+='</tbody></table>';
  html+='<div style="font-size:11px;color:#666;margin-top:6px">中位数: 主'+m.odds.home.toFixed(2)+' '+(bb?'':'平'+m.odds.draw.toFixed(2)+' ')+'客'+m.odds.away.toFixed(2)+' | 最高赔率已标绿</div>';
  html+='</div>';

  // 进球数大小球指数
  if(!bb&&m.totals){
    var t=m.totals;
    html+='<div class="modal-section"><h4>进球数大小球指数</h4>';
    html+='<div class="detail-grid">';
    html+='<div class="detail-card"><div class="label">盘口</div><div class="value">'+t.point+'球</div></div>';
    html+='<div class="detail-card"><div class="label">大球赔率</div><div class="value green">'+t.med_over.toFixed(2)+'</div></div>';
    html+='<div class="detail-card"><div class="label">小球赔率</div><div class="value red">'+t.med_under.toFixed(2)+'</div></div>';
    html+='<div class="detail-card"><div class="label">大球概率(去水)</div><div class="value '+(t.p_over>0.5?'green':'red')+'">'+(t.p_over*100).toFixed(1)+'%</div></div>';
    html+='</div>';
    if(t.companies&&Object.keys(t.companies).length>0){
      html+='<table class="odds-table" style="margin-top:10px"><thead><tr><th>博彩公司</th><th>盘口</th><th>大球</th><th>小球</th></tr></thead><tbody>';
      Object.keys(t.companies).forEach(function(cn){
        var c=t.companies[cn];
        html+='<tr><td>'+cn+'</td><td>'+c.point+'</td><td>'+c.over.toFixed(2)+'</td><td>'+c.under.toFixed(2)+'</td></tr>';
      });
      html+='</tbody></table>';
    }
    html+='</div>';
  }

  // 角球数量预估
  if(!bb&&m.corners){
    var c=m.corners;
    html+='<div class="modal-section"><h4>角球数量预估</h4>';
    html+='<div class="detail-grid">';
    html+='<div class="detail-card"><div class="label">预期角球数</div><div class="value orange">'+c.expected+'个</div></div>';
    html+='<div class="detail-card"><div class="label">最可能角球数</div><div class="value">'+c.most_likely+'个 ('+(c.most_likely_prob*100).toFixed(1)+'%)</div></div>';
    html+='<div class="detail-card"><div class="label">大9.5角概率</div><div class="value '+(c.p_over>0.5?'green':'red')+'">'+(c.p_over*100).toFixed(1)+'%</div></div>';
    html+='<div class="detail-card"><div class="label">联赛平均</div><div class="value">'+c.league_avg+'个</div></div>';
    html+='</div>';
    // 角球分布柱状图
    html+='<div style="margin-top:16px;padding:0 20px"><div class="corner-dist">';
    c.distribution.forEach(function(d){
      var h=Math.max(2,d.prob*300);
      html+='<div class="corner-bar" style="height:'+h+'px" title="'+d.corners+'个: '+(d.prob*100).toFixed(1)+'%">';
      if(d.prob>0.05)html+='<span class="cval">'+(d.prob*100).toFixed(0)+'%</span>';
      html+='<span class="clabel">'+d.corners+'</span></div>';
    });
    html+='</div><div style="text-align:center;font-size:11px;color:#999;margin-top:20px">角球数量分布（泊松模型，λ='+c.expected+'）</div></div>';
    html+='</div>';
  }

  // 泊松比分预测
  if(!bb&&m.poisson&&m.poisson.matrix){
    html+='<div class="modal-section"><h4>泊松比分预测矩阵</h4>';
    html+='<div style="font-size:11px;color:#666;margin-bottom:8px">最可能比分: <b>'+m.poisson.score+'</b> ('+(m.poisson.prob*100).toFixed(1)+'%) | 行=主队进球, 列=客队进球</div>';
    html+='<div class="poisson-matrix">';
    html+='<div class="poisson-cell" style="background:#004b81;color:#fff">主\\客</div>';
    for(var j=0;j<5;j++)html+='<div class="poisson-cell" style="background:#004b81;color:#fff">'+j+'</div>';
    for(var i=0;i<5;i++){
      html+='<div class="poisson-cell" style="background:#004b81;color:#fff">'+i+'</div>';
      for(var j=0;j<5;j++){
        var cell=m.poisson.matrix.find(function(s){return s.home===i&&s.away===j});
        var prob=cell?cell.prob:0;
        var hl=cell&&(cell.home+'-'+cell.away)===m.poisson.score?'highlight':'';
        html+='<div class="poisson-cell '+hl+'" title="'+i+'-'+j+': '+(prob*100).toFixed(2)+'%">'+(prob*100).toFixed(1)+'%</div>';
      }
    }
    html+='</div></div>';
  }

  // 凯利指数与Edge
  html+='<div class="modal-section"><h4>凯利指数与价值评估</h4>';
  html+='<div class="detail-grid">';
  html+='<div class="detail-card"><div class="label">主胜Edge</div><div class="value '+(m.edge.home>2?'green':m.edge.home>0?'orange':'red')+'">'+(m.edge.home>0?'+':'')+m.edge.home.toFixed(2)+'%</div></div>';
  if(!bb)html+='<div class="detail-card"><div class="label">平局Edge</div><div class="value '+(m.edge.draw>2?'green':m.edge.draw>0?'orange':'red')+'">'+(m.edge.draw>0?'+':'')+m.edge.draw.toFixed(2)+'%</div></div>';
  html+='<div class="detail-card"><div class="label">客胜Edge</div><div class="value '+(m.edge.away>2?'green':m.edge.away>0?'orange':'red')+'">'+(m.edge.away>0?'+':'')+m.edge.away.toFixed(2)+'%</div></div>';
  html+='<div class="detail-card"><div class="label">主胜凯利</div><div class="value">'+m.kelly.home.toFixed(3)+'</div></div>';
  if(!bb)html+='<div class="detail-card"><div class="label">平局凯利</div><div class="value">'+m.kelly.draw.toFixed(3)+'</div></div>';
  html+='<div class="detail-card"><div class="label">客胜凯利</div><div class="value">'+m.kelly.away.toFixed(3)+'</div></div>';
  html+='</div>';
  var maxEdge=Math.max(m.edge.home,m.edge.away,bb?-99:m.edge.draw);
  if(maxEdge>2){
    html+='<div style="margin-top:10px;padding:8px;background:#d4edda;border-radius:4px;color:#155724;font-size:12px">✓ 发现价值投注机会！最大Edge +'+maxEdge.toFixed(2)+'%，建议关注。</div>';
  }else if(maxEdge>0){
    html+='<div style="margin-top:10px;padding:8px;background:#fff3cd;border-radius:4px;color:#856404;font-size:12px">⚠ 存在轻微正EV（+'+maxEdge.toFixed(2)+'%），但未达2%阈值，需谨慎。</div>';
  }else{
    html+='<div style="margin-top:10px;padding:8px;background:#f8d7da;border-radius:4px;color:#721c24;font-size:12px">✗ 当前无价值投注机会，市场定价较为有效。</div>';
  }
  html+='</div>';

  document.getElementById('modalBody').innerHTML=html;
  document.getElementById('matchModal').classList.add('show');
}

function initCharts(){
  if(typeof echarts==='undefined'){setTimeout(initCharts,300);return}
  var fb=MD.filter(function(m){return m.sport==='football'}).slice(0,10);
  var bb=MD.filter(function(m){return m.sport==='basketball'}).slice(0,5);
  var data=fb.concat(bb).slice(0,12);
  if(!data.length){
    document.querySelectorAll('.cc').forEach(function(el){el.innerHTML='<div style="text-align:center;padding:60px;color:#999">暂无数据</div>'});
    return;
  }
  var labels=data.map(function(m){return m.home.substring(0,6)+'v'+m.away.substring(0,6)});
  var tt={trigger:'axis',backgroundColor:'#fff',borderColor:'#ccc',textStyle:{color:'#333',fontSize:12}};
  var leg={bottom:0,left:'center',textStyle:{fontSize:mob?10:12}};
  var grd={top:mob?'14%':'10%',bottom:'18%',left:'8%',right:'5%',containLabel:true};

  var c1=echarts.init(document.getElementById('c1'));
  c1.setOption({tooltip:tt,legend:leg,grid:grd,
    xAxis:{type:'category',data:labels,axisLabel:{color:'#666',fontSize:mob?9:11,rotate:mob?35:20}},
    yAxis:{type:'value',max:1,axisLabel:{color:'#666',fontSize:10,formatter:function(v){return(v*100)+'%'}},splitLine:{lineStyle:{color:'#e0e0e0'}}},
    series:[
      {name:'市场-主胜',type:'bar',itemStyle:{color:'rgba(26,109,181,.3)'},barGap:'5%',data:data.map(function(m){return m.market.home})},
      {name:'后验-主胜',type:'bar',itemStyle:{color:'#1a6db5'},data:data.map(function(m){return m.bayes.home})},
      {name:'市场-客胜',type:'bar',itemStyle:{color:'rgba(198,40,40,.3)'},barGap:'5%',data:data.map(function(m){return m.market.away})},
      {name:'后验-客胜',type:'bar',itemStyle:{color:'#c62828'},data:data.map(function(m){return m.bayes.away})}
    ]});

  var hc=data.filter(function(m){return m.companies&&Object.keys(m.companies).length>1});
  var cl2=hc.slice(0,8).map(function(m){return m.home.substring(0,6)});
  var cn=[];hc.forEach(function(m){Object.keys(m.companies).forEach(function(n){if(cn.indexOf(n)<0)cn.push(n)})});
  var cs=['#1a6db5','#2e7d32','#e65100','#c62828','#00695c','#7b1fa2','#00838f','#ef6c00'];
  var s2=cn.slice(0,8).map(function(n,i){return{name:n.substring(0,10),type:'bar',itemStyle:{color:cs[i%8]},barGap:'8%',data:hc.slice(0,8).map(function(m){return m.companies[n]?m.companies[n].home:null})}});
  var c2=echarts.init(document.getElementById('c2'));
  c2.setOption({tooltip:tt,legend:leg,grid:grd,
    xAxis:{type:'category',data:cl2,axisLabel:{color:'#666',fontSize:mob?9:11}},
    yAxis:{type:'value',axisLabel:{color:'#666',fontSize:10},splitLine:{lineStyle:{color:'#e0e0e0'}}},
    series:s2});

  var sv=VB.slice(0,15).reverse();
  var c3=echarts.init(document.getElementById('c3'));
  if(sv.length){
    c3.setOption({tooltip:{trigger:'axis',backgroundColor:'#fff',borderColor:'#ccc',textStyle:{color:'#333',fontSize:12},formatter:function(p){var d=sv[p[0].dataIndex];return'<b>'+d.n+'</b><br />Edge: <b style="color:#2e7d32">+'+d.e.toFixed(1)+'%</b><br />贝叶斯: '+(d.p*100).toFixed(1)+'%<br />最高赔率: '+d.odds+'<br />期望收益: '+((d.p*d.odds-1)*100).toFixed(1)+'%'}},
      grid:{top:'5%',bottom:'8%',left:mob?'32%':'25%',right:'10%'},
      xAxis:{type:'value',axisLabel:{color:'#666',fontSize:10,formatter:function(v){return'+'+v+'%'}},splitLine:{lineStyle:{color:'#e0e0e0'}}},
      yAxis:{type:'category',data:sv.map(function(d){return d.n}),axisLabel:{color:'#333',fontSize:mob?9:11,width:mob?90:140,overflow:'truncate'}},
      series:[{type:'bar',data:sv.map(function(d){return d.e}),itemStyle:{color:function(p){return p.value>5?'#2e7d32':p.value>3?'#1a6db5':'#f5a623'}},barWidth:'60%',label:{show:true,position:'right',fontSize:mob?9:11,color:'#333',fontFamily:'Tahoma',formatter:function(p){return'+'+p.value.toFixed(1)+'%'}}}]});
  }else{
    document.getElementById('c3').innerHTML='<div style="text-align:center;padding:60px;color:#999">当前无 Edge&gt;2% 的机会</div>';
  }

  var c4=echarts.init(document.getElementById('c4'));
  c4.setOption({tooltip:tt,legend:leg,grid:grd,
    xAxis:{type:'category',data:labels,axisLabel:{color:'#666',fontSize:mob?9:11}},
    yAxis:{type:'value',min:0,max:0.15,axisLabel:{color:'#666',fontSize:10,formatter:function(v){return(v*100).toFixed(0)+'%'}},splitLine:{lineStyle:{color:'#e0e0e0'}}},
    series:[
      {name:'主胜 f*',type:'bar',itemStyle:{color:'#2e7d32'},barGap:'10%',data:data.map(function(m){return m.kelly.home})},
      {name:'平局 f*',type:'bar',itemStyle:{color:'#f5a623'},data:data.map(function(m){return m.sport==='basketball'?0:m.kelly.draw})},
      {name:'客胜 f*',type:'bar',itemStyle:{color:'#c62828'},data:data.map(function(m){return m.kelly.away})}
    ]});

  var fm=MD.filter(function(m){return m.sport==='football'})[0];
  var mx=fm?fm.poisson.matrix:[];
  var hg=['0','1','2','3','4','5'],ag=['0','1','2','3','4','5'];
  var hd=mx.map(function(s){return[s.home,s.away,parseFloat((s.prob*100).toFixed(2))]});
  var c5=echarts.init(document.getElementById('c5'));
  if(hd.length){
    c5.setOption({tooltip:{backgroundColor:'#fff',borderColor:'#ccc',textStyle:{color:'#333',fontSize:12},formatter:function(p){var v=p.value;return'比分 '+v[0]+'-'+v[1]+'<br />概率: <b>'+v[2].toFixed(2)+'%</b>'}},
      grid:{top:'8%',bottom:'14%',left:'12%',right:'10%'},
      xAxis:{type:'category',data:hg,name:(fm?fm.home:'主队')+'进球',nameTextStyle:{color:'#666',fontSize:10},axisLabel:{color:'#666',fontSize:mob?9:11}},
      yAxis:{type:'category',data:ag,name:(fm?fm.away:'客队')+'进球',nameTextStyle:{color:'#666',fontSize:10},axisLabel:{color:'#666',fontSize:mob?9:11}},
      visualMap:{min:0,max:15,orient:'horizontal',left:'center',bottom:0,inRange:{color:['#f5f5f5','#c8e6c9','#66bb6a','#2e7d32','#1b5e20']},textStyle:{color:'#666',fontSize:10}},
      series:[{type:'heatmap',data:hd,label:{show:true,fontSize:mob?8:10,color:'#333',formatter:function(p){return p.value[2]>=1?p.value[2].toFixed(1):''}},emphasis:{itemStyle:{shadowBlur:8,shadowColor:'rgba(0,0,0,.2)'}}}]});
  }else{
    document.getElementById('c5').innerHTML='<div style="text-align:center;padding:60px;color:#999">暂无足球赛事</div>';
  }

  var c6=echarts.init(document.getElementById('c6'));
  c6.setOption({tooltip:tt,legend:leg,grid:grd,
    xAxis:{type:'category',data:labels,axisLabel:{color:'#666',fontSize:mob?9:11}},
    yAxis:{type:'value',min:0.85,max:1.2,axisLabel:{color:'#666',fontSize:10},splitLine:{lineStyle:{color:'#e0e0e0'}}},
    series:[
      {name:'主胜比值',type:'bar',itemStyle:{color:'#2e7d32'},barGap:'10%',data:data.map(function(m){return m.kelly_ratio.home})},
      {name:'平局比值',type:'bar',itemStyle:{color:'#f5a623'},data:data.map(function(m){return m.sport==='basketball'?0:m.kelly_ratio.draw})},
      {name:'客胜比值',type:'bar',itemStyle:{color:'#c62828'},data:data.map(function(m){return m.kelly_ratio.away})},
      {name:'正EV线(1.0)',type:'line',data:labels.map(function(){return 1}),lineStyle:{color:'#999',type:'dashed'},symbol:'none'},
      {name:'强信号(1.05)',type:'line',data:labels.map(function(){return 1.05}),lineStyle:{color:'#c62828',type:'dashed'},symbol:'none'}
    ]});

  window.addEventListener('resize',function(){c1.resize();c2.resize();c3.resize();c4.resize();c5.resize();c6.resize()});
}
initCharts();
</script>

<!-- 比赛详情弹窗 -->
<div class="modal-overlay" id="matchModal" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="modal-header">
      <div>
        <h3 id="modalTitle">比赛详情</h3>
        <span class="league-tag" id="modalLeague"></span>
        <span style="font-size:11px;opacity:0.8;margin-left:8px" id="modalTime"></span>
      </div>
      <button class="modal-close" onclick="closeModal()">&times;</button>
    </div>
    <div class="modal-body" id="modalBody">
      <!-- 动态内容 -->
    </div>
  </div>
</div>

</body>
</html>'''


def generate_html(data):
    data_json = json.dumps(data, ensure_ascii=False)
    html = get_html_template()
    html = html.replace('__DATA_PLACEHOLDER__', data_json)
    return html


def main():
    global requests_remaining
    if not API_KEY:
        print('ERROR: ODDS_API_KEY not set')
        sys.exit(1)

    now = datetime.now(timezone.utc)
    update_time = now.strftime('%Y-%m-%d %H:%M UTC')
    print(f'=== 赛事赔率贝叶斯分析看板 v7 (Elo增强版) ===')
    print(f'时间: {update_time}')

    # 获取ClubElo足球球队评分
    print('\n--- 获取球队Elo数据 ---')
    football_elo_data = fetch_clubelo_rankings()

    # 动态获取所有活跃联赛
    print('\n--- 获取全球联赛列表 ---')
    all_sports = fetch_all_sports()
    if not all_sports:
        print('WARNING: 无法获取联赛列表, 使用默认联赛')
        all_sports = [{'key': k, 'title': k, 'active': True} for k in FOOTBALL_SPORTS + BASKETBALL_SPORTS]

    # 筛选足球和篮球联赛, 按优先级排序
    # 只请求优先级<=4的联赛, 节省API配额(免费版每月500次)
    MAX_FOOTBALL_LEAGUES = 15
    MAX_BASKETBALL_LEAGUES = 8
    football_leagues = sorted(
        [s for s in all_sports if s['key'].startswith('soccer_') and get_sport_priority(s['key']) <= 4],
        key=lambda s: get_sport_priority(s['key'])
    )[:MAX_FOOTBALL_LEAGUES]
    basketball_leagues = sorted(
        [s for s in all_sports if s['key'].startswith('basketball_') and get_sport_priority(s['key']) <= 4],
        key=lambda s: get_sport_priority(s['key'])
    )[:MAX_BASKETBALL_LEAGUES]
    print(f'足球联赛(优先级<=4, 最多{MAX_FOOTBALL_LEAGUES}个): {len(football_leagues)} 个')
    print(f'篮球联赛(优先级<=4, 最多{MAX_BASKETBALL_LEAGUES}个): {len(basketball_leagues)} 个')
    print(f'足球联赛: {[s["key"] for s in football_leagues]}')
    print(f'篮球联赛: {[s["key"] for s in basketball_leagues]}')

    print('\n--- 足球赛事 (全球) ---')
    football_events = []
    for sport in football_leagues:
        if len(football_events) >= MAX_FOOTBALL_MATCHES:
            print(f'  已达到足球上限 {MAX_FOOTBALL_MATCHES} 场, 停止抓取')
            break
        if requests_remaining < MIN_REQUESTS_REMAINING:
            print(f'  剩余请求不足 ({requests_remaining}), 停止抓取')
            break
        events = fetch_odds(sport['key'])
        football_events.extend(events)

    print('\n--- 篮球赛事 (全球) ---')
    basketball_events = []
    for sport in basketball_leagues:
        if len(basketball_events) >= MAX_BASKETBALL_MATCHES:
            print(f'  已达到篮球上限 {MAX_BASKETBALL_MATCHES} 场, 停止抓取')
            break
        if requests_remaining < MIN_REQUESTS_REMAINING:
            print(f'  剩余请求不足 ({requests_remaining}), 停止抓取')
            break
        events = fetch_odds(sport['key'])
        basketball_events.extend(events)

    print(f'\n抓取汇总: 足球 {len(football_events)} 场 / 篮球 {len(basketball_events)} 场 / 剩余请求 {requests_remaining}')

    # 按开赛时间排序
    football_events.sort(key=lambda e: e.get('commence_time', ''))
    basketball_events.sort(key=lambda e: e.get('commence_time', ''))
    print(f'已按开赛时间排序')

    # 标记The Odds API数据来源
    for e in football_events:
        e['source'] = 'theoddsapi'
    for e in basketball_events:
        e['source'] = 'theoddsapi'

    # ========== 多源数据补充: 澳客网爬虫(免费, 无需API Key) ==========
    if OKOOO_AVAILABLE:
        print('\n--- 澳客网爬虫补充数据 (免费源) ---')
        try:
            okooo_football = okooo_scraper.fetch_football_matches(days_ahead=7)
            if okooo_football:
                print(f'澳客网获取到 {len(okooo_football)} 场足球比赛')
                # 合并去重 (The Odds API优先, 澳客网补充)
                football_events = merge_and_deduplicate(football_events, okooo_football)
                # 重新按开赛时间排序
                football_events.sort(key=lambda e: e.get('commence_time', ''))
            else:
                print('澳客网未获取到比赛数据')
        except Exception as e:
            print(f'澳客网爬虫出错: {e}')
    else:
        print('\n--- 澳客网爬虫模块不可用, 跳过 ---')

    # 篮球简易Elo（基于市场赔率反推）
    basketball_elo_data = estimate_basketball_elo_from_odds(basketball_events)

    print('\n--- 贝叶斯分析 (v7: Elo先验 + 市场似然 + 泊松Elo调整) ---')
    football_data = bayesian_analysis(football_events, 'football', elo_data=football_elo_data)
    basketball_data = bayesian_analysis(basketball_events, 'basketball', elo_data=basketball_elo_data)
    print(f'足球: {len(football_data)} 场分析完成')
    # 统计Elo使用率
    if football_data:
        elo_used = sum(1 for d in football_data if d.get('elo', {}).get('used'))
        print(f'  其中Elo先验生效: {elo_used}/{len(football_data)} 场 ({elo_used*100//max(1,len(football_data))}%)')
    print(f'篮球: {len(basketball_data)} 场分析完成')
    if basketball_data:
        elo_used = sum(1 for d in basketball_data if d.get('elo', {}).get('used'))
        print(f'  其中Elo先验生效: {elo_used}/{len(basketball_data)} 场 ({elo_used*100//max(1,len(basketball_data))}%)')

    if not football_data and not basketball_data:
        print('WARNING: No data to generate dashboard')
        sys.exit(0)

    value_count = 0
    ev_count = 0
    for m in football_data + basketball_data:
        is_bb = m['sport'] == 'basketball'
        edges = [m['edge']['home'], m['edge']['away']]
        if not is_bb:
            edges.append(m['edge']['draw'])
        if max(edges) > 2:
            value_count += 1
        if m['kelly_ratio']['home'] > 1:
            ev_count += 1
        if m['kelly_ratio']['away'] > 1:
            ev_count += 1
        if not is_bb and m['kelly_ratio']['draw'] > 1:
            ev_count += 1

    print(f'\n价值投注 (Edge>2%): {value_count} 个')
    print(f'正EV机会 (kelly_ratio>1): {ev_count} 个')

    print('\n--- 数据验证 (前3场) ---')
    for m in (football_data + basketball_data)[:3]:
        print(f"  {m['match']}:")
        print(f"    赔率(中位/最高): 主{m['odds']['home']}/{m['odds']['best_home']} 客{m['odds']['away']}/{m['odds']['best_away']}")
        print(f"    市场概率: 主{m['market']['home']:.4f} 客{m['market']['away']:.4f}")
        print(f"    贝叶斯后验: 主{m['bayes']['home']:.4f} 客{m['bayes']['away']:.4f}")
        print(f"    Edge: 主{m['edge']['home']:.2f}% 客{m['edge']['away']:.2f}%")
        print(f"    凯利f*: 主{m['kelly']['home']} 客{m['kelly']['away']}")
        print(f"    凯利比值: 主{m['kelly_ratio']['home']} 客{m['kelly_ratio']['away']}")
        if m['poisson']['top5']:
            top3_str = ', '.join(
                f"{s['home']}-{s['away']}({s['prob']*100:.1f}%)"
                for s in m['poisson']['top5'][:3]
            )
            print(f"    泊松Top3: {top3_str}")

    print('\n--- 生成看板 ---')
    all_data = {
        'update_time': update_time,
        'football': football_data,
        'basketball': basketball_data,
    }
    html = generate_html(all_data)

    output_path = os.path.join(REPO_DIR, 'index.html')
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'看板已写入: {output_path} ({len(html)//1024}KB)')

    nojekyll = os.path.join(REPO_DIR, '.nojekyll')
    with open(nojekyll, 'w') as f:
        f.write('')

    data_path = os.path.join(REPO_DIR, 'data.json')
    with open(data_path, 'w', encoding='utf-8') as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)
    print(f'数据已保存: {data_path}')

    print('\n=== 更新完成 ===')


if __name__ == '__main__':
    main()
