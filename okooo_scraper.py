#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
澳客网(okooo.com)爬虫模块
从比赛列表页获取足球赛事的胜平负赔率和亚盘数据
无需API Key，完全免费
"""

import re
import time
import logging
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError

logger = logging.getLogger(__name__)

# 澳客网联赛ID -> 联赛名称映射(常用联赛)
LEAGUE_MAP = {
    '36': '英超', '37': '英冠', '38': '英甲', '39': '英乙',
    '34': '西甲', '35': '西乙',
    '8': '德甲', '9': '德乙',
    '17': '意甲', '18': '意乙',
    '23': '法甲', '24': '法乙',
    '53': '荷甲', '54': '荷乙',
    '44': '葡超', '45': '葡甲',
    '203': '俄超', '204': '俄甲',
    '152': '土超', '153': '土甲',
    '182': '比甲', '183': '比乙',
    '218': '苏超', '219': '苏冠',
    '238': '奥甲', '239': '奥乙',
    '131': '瑞士超', '132': '瑞士甲',
    '480': '南美杯', '481': '南美解放者杯',
    '390': '巴西乙', '391': '巴西甲',
    '241': '哥伦甲', '242': '哥伦乙',
    '352': '墨联', '353': '墨甲',
    '474': '亚运男足',
    '110617': '巴西乙',  # 赛季ID
}

# 亚盘盘口文本 -> 数值映射
HANDICAP_MAP = {
    '平手': 0, '平手/半球': -0.25, '半球': -0.5, '半球/一球': -0.75,
    '一球': -1, '一球/球半': -1.25, '球半': -1.5, '球半/两球': -1.75,
    '两球': -2, '两球/两球半': -2.25, '两球半': -2.5,
    '受平手': 0, '受平手/半球': 0.25, '受半球': 0.5, '受半球/一球': 0.75,
    '受一球': 1, '受一球/球半': 1.25, '受球半': 1.5, '受球半/两球': 1.75,
    '受两球': 2,
}


def fetch_okooo_page(url, referer='https://www.okooo.com/', timeout=20):
    """获取澳客网页面，处理gb2312编码"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Referer': referer,
    }
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            # 尝试gb2312解码，失败则用utf-8
            try:
                return raw.decode('gb2312', errors='ignore')
            except:
                return raw.decode('utf-8', errors='ignore')
    except Exception as e:
        logger.error(f'澳客网页面获取失败 {url}: {e}')
        return ''


def parse_okooo_matches(html):
    """解析澳客网比赛列表页，返回统一格式的比赛数据"""
    matches = []
    if not html:
        return matches

    # 匹配所有比赛行
    pattern = r'<tr attr="([^"]*)" class="trclassobj">(.*?)</tr>'
    rows = re.findall(pattern, html, re.DOTALL)

    for attr, row in rows:
        try:
            match = parse_single_match(attr, row)
            if match:
                matches.append(match)
        except Exception as e:
            logger.debug(f'解析比赛行失败: {e}')
            continue

    return matches


def parse_single_match(attr, row):
    """解析单场比赛数据"""
    # attr格式: 联赛ID,时间戳,亚盘盘口,其他
    attr_parts = attr.split(',')
    if len(attr_parts) < 2:
        return None

    league_id = attr_parts[0]
    timestamp = int(attr_parts[1]) if attr_parts[1].isdigit() else 0
    handicap_point = float(attr_parts[2]) if len(attr_parts) > 2 and attr_parts[2] else 0

    # 转换时间戳为ISO格式
    if timestamp > 0:
        commence_time = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    else:
        commence_time = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    # 提取联赛名称
    league_match = re.search(r'<a[^>]*href="/soccer/league/\d+/[^"]*"[^>]*>([^<]+)</a>', row)
    league_name = league_match.group(1).strip() if league_match else LEAGUE_MAP.get(league_id, f'联赛{league_id}')

    # 提取比赛时间(显示用)
    time_match = re.search(r'<td width="5%">([^<]+)</td>', row)
    match_time_display = time_match.group(1).strip() if time_match else ''

    # 提取主队和客队
    teams = re.findall(r'<a href="/soccer/team/\d+/[^"]*"[^>]*>([^<]+)</a>', row)
    if len(teams) < 2:
        return None
    home_team = teams[0].strip()
    away_team = teams[1].strip()

    # 提取比赛ID
    match_id_match = re.search(r'href="/soccer/match/(\d+)/"', row)
    match_id = match_id_match.group(1) if match_id_match else f'okooo_{league_id}_{timestamp}'

    # 提取赔率数字 (胜平负 + 亚盘水位)
    odds = re.findall(r'<td width="5%">([0-9.]+)</td>', row)
    if len(odds) < 3:
        return None

    home_odds = float(odds[0])
    draw_odds = float(odds[1])
    away_odds = float(odds[2])

    # 亚盘水位
    handicap_home = float(odds[3]) if len(odds) > 3 else 0
    handicap_away = float(odds[4]) if len(odds) > 4 else 0

    # 亚盘盘口文本
    handicap_text_match = re.search(r'<td>([^<]+)</td>', row)
    handicap_text = handicap_text_match.group(1).strip() if handicap_text_match else ''

    # 构造统一格式(兼容The Odds API格式)
    # 注意: h2h市场的outcomes名称必须使用球队名称, 与The Odds API保持一致
    result = {
        'id': f'okooo_{match_id}',
        'sport_key': f'soccer_okooo_{league_id}',
        'sport_title': league_name,
        'commence_time': commence_time,
        'home_team': home_team,
        'away_team': away_team,
        'bookmakers': [{
            'key': 'okooo',
            'title': '澳客网',
            'markets': [
                {
                    'key': 'h2h',
                    'outcomes': [
                        {'name': home_team, 'price': home_odds},
                        {'name': 'Draw', 'price': draw_odds},
                        {'name': away_team, 'price': away_odds}
                    ]
                }
            ]
        }],
        # 额外字段(澳客网特有)
        'source': 'okooo',
        'league_id': league_id,
        'match_id': match_id,
        'handicap': {
            'point': handicap_point,
            'text': handicap_text,
            'home_water': handicap_home,
            'away_water': handicap_away
        }
    }

    # 如果有亚盘数据，添加spreads市场
    if handicap_home > 0 and handicap_away > 0:
        result['bookmakers'][0]['markets'].append({
            'key': 'spreads',
            'outcomes': [
                {'name': 'Home', 'price': handicap_home, 'point': -handicap_point if handicap_point < 0 else handicap_point},
                {'name': 'Away', 'price': handicap_away, 'point': handicap_point if handicap_point > 0 else -handicap_point}
            ]
        })

    return result


def fetch_football_matches(days_ahead=3):
    """
    获取澳客网足球比赛列表
    返回统一格式的比赛数据数组
    """
    url = 'https://www.okooo.com/soccer/match/'
    logger.info(f'正在从澳客网获取足球比赛数据...')

    html = fetch_okooo_page(url)
    if not html:
        logger.error('澳客网页面获取失败')
        return []

    matches = parse_okooo_matches(html)
    logger.info(f'澳客网获取到 {len(matches)} 场足球比赛')

    # 按时间过滤(只保留未来days_ahead天内的比赛)
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now + days_ahead * 86400
    filtered = []
    for m in matches:
        try:
            t = datetime.strptime(m['commence_time'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
            if now - 3600 <= t <= cutoff:  # 包含1小时内正在进行的比赛
                filtered.append(m)
        except:
            continue

    logger.info(f'过滤后剩余 {len(filtered)} 场比赛(未来{days_ahead}天内)')
    return filtered


def fetch_basketball_matches():
    """获取澳客网篮球比赛列表(篮球赛季未开始时可能为空)"""
    url = 'https://www.okooo.com/basketball/'
    logger.info('正在从澳客网获取篮球比赛数据...')

    html = fetch_okooo_page(url)
    if not html:
        return []

    # 篮球页面结构可能不同，需要单独解析
    # 暂时返回空，后续完善
    logger.info('澳客网篮球数据暂未实现或赛季未开始')
    return []


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    matches = fetch_football_matches()
    print(f'\n共获取 {len(matches)} 场比赛')
    for m in matches[:5]:
        print(f"\n{m['sport_title']} {m['commence_time']}")
        print(f"  {m['home_team']} vs {m['away_team']}")
        h2h = m['bookmakers'][0]['markets'][0]['outcomes']
        print(f"  欧赔: {h2h[0]['price']} / {h2h[1]['price']} / {h2h[2]['price']}")
        if m.get('handicap', {}).get('text'):
            print(f"  亚盘: {m['handicap']['text']} ({m['handicap']['home_water']}/{m['handicap']['away_water']})")
