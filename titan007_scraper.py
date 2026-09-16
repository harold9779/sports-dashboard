#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
球探网(titan007.com)爬虫模块
从赛程赛果页获取足球比赛列表(联赛/时间/球队)
注意: 球探网赔率页面反爬严格(500错误), 本模块仅获取比赛列表
赔率数据需与澳客网等其他源匹配
"""

import re
import time
import random
import logging
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError

logger = logging.getLogger(__name__)

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
]


def fetch_titan_page(url, referer='http://www.titan007.com/', timeout=20, max_retries=3):
    """获取球探网页面，处理gb2312编码，带重试"""
    for attempt in range(max_retries):
        headers = {
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Referer': referer,
            'Connection': 'keep-alive',
        }
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    return raw.decode('gb2312', errors='ignore')
                except:
                    return raw.decode('utf-8', errors='ignore')
        except HTTPError as e:
            if e.code in [403, 405, 500]:
                wait_time = 5 * (attempt + 1)
                logger.warning(f'球探网错误({e.code})，等待{wait_time}秒后重试({attempt+1}/{max_retries})')
                time.sleep(wait_time)
                continue
            logger.error(f'球探网HTTP错误 {e.code}: {url}')
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            return ''
        except Exception as e:
            logger.error(f'球探网页面获取失败 {url}: {e}')
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            return ''
    return ''


def parse_titan_schedule(html):
    """解析球探网赛程页，返回比赛列表"""
    matches = []
    if not html:
        return matches

    # 匹配比赛行 (包含sId的tr)
    pattern = r"<tr[^>]*id='tr1_\d+'[^>]*sId='(\d+)'[^>]*>(.*?)</tr>"
    rows = re.findall(pattern, html, re.DOTALL)

    for match_id, row in rows:
        try:
            match = parse_single_match(match_id, row)
            if match:
                matches.append(match)
        except Exception as e:
            logger.debug(f'解析球探网比赛行失败: {e}')
            continue

    return matches


def parse_single_match(match_id, row):
    """解析单场比赛数据"""
    # 提取联赛名称
    league_match = re.search(r"<span>([^<]+)</span>", row)
    league_name = league_match.group(1).strip() if league_match else '未知联赛'

    # 提取比赛时间 (格式: 9-17 13:00)
    time_match = re.search(r"<td>(\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2})</td>", row)
    if not time_match:
        return None
    time_str = time_match.group(1).strip()

    # 转换为完整的ISO时间
    try:
        # 解析月-日 时:分
        month_day, hm = time_str.split()
        month, day = map(int, month_day.split('-'))
        hour, minute = map(int, hm.split(':'))

        # 年份处理 (球探网赛程页是未来1-2天的比赛)
        now = datetime.now(timezone.utc)
        year = now.year
        # 如果月份小于当前月份很多，可能是下一年
        if month < now.month - 1:
            year += 1

        match_dt = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
        commence_time = match_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    except:
        commence_time = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    # 提取主队 (align=right的td)
    home_match = re.search(r"<td align=right>(.*?)</td>", row, re.DOTALL)
    if not home_match:
        return None
    home_html = home_match.group(1)
    # 去除排名标签和多余标签
    home_team = re.sub(r"<[^>]+>", "", home_html).strip()
    home_team = re.sub(r"\s+", " ", home_team).strip()
    # 去除排名标签如 [6] 或 [8]
    home_team = re.sub(r"\[\d+\]", "", home_team).strip()
    # 去除(中)等中立场地标记
    home_team = re.sub(r"\(中\)$", "", home_team).strip()

    # 提取客队 (align=left的td，在比分之后)
    away_match = re.search(r"<td align=left>(.*?)</td>", row, re.DOTALL)
    if not away_match:
        return None
    away_html = away_match.group(1)
    away_team = re.sub(r"<[^>]+>", "", away_html).strip()
    away_team = re.sub(r"\s+", " ", away_team).strip()
    # 去除排名标签如 [6] 或 [8]
    away_team = re.sub(r"\[\d+\]", "", away_team).strip()

    if not home_team or not away_team:
        return None

    # 提取联赛ID (从name属性)
    league_id_match = re.search(r"name='(\d+),", row)
    league_id = league_id_match.group(1) if league_id_match else '0'

    # 构造比赛数据 (仅列表信息，无赔率)
    result = {
        'id': f'titan_{match_id}',
        'sport_key': f'soccer_titan_{league_id}',
        'sport_title': league_name,
        'commence_time': commence_time,
        'home_team': home_team,
        'away_team': away_team,
        'bookmakers': [],  # 球探网赔率获取困难，留空
        'source': 'titan007',
        'match_id': match_id,
        'league_id': league_id,
        'has_odds': False,  # 标记无赔率
    }

    return result


def fetch_football_matches(days_ahead=2):
    """
    获取球探网足球比赛列表
    球探网赛程页默认显示明天的比赛
    返回比赛列表(无赔率数据，需与其他源匹配)
    """
    all_matches = []

    # 获取今天和明天的赛程
    for day_offset in range(days_ahead):
        target_date = datetime.now(timezone.utc) + timedelta(days=day_offset)
        date_str = target_date.strftime('%Y%m%d')
        url = f'http://bf.titan007.com/football/Next_{date_str}.htm'

        logger.info(f'正在从球探网获取 {date_str} 足球比赛列表...')
        html = fetch_titan_page(url)

        if not html:
            logger.warning(f'球探网 {date_str} 页面获取失败')
            continue

        matches = parse_titan_schedule(html)
        logger.info(f'球探网 {date_str} 获取到 {len(matches)} 场比赛')
        all_matches.extend(matches)

    # 去重 (按match_id)
    seen = set()
    unique = []
    for m in all_matches:
        mid = m.get('match_id', '')
        if mid and mid not in seen:
            seen.add(mid)
            unique.append(m)

    logger.info(f'球探网总共获取 {len(unique)} 场比赛(去重后)')
    return unique


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    matches = fetch_football_matches(days_ahead=2)
    print(f'\n共获取 {len(matches)} 场比赛')
    for m in matches[:10]:
        print(f"\n{m['sport_title']} {m['commence_time']}")
        print(f"  {m['home_team']} vs {m['away_team']}")
        print(f"  ID: {m['match_id']}, 有赔率: {m['has_odds']}")
