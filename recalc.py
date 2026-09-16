#!/usr/bin/env python3
"""
用最新概率引擎重算现有 data.json（无需联网）。
从已保存的各公司赔率反向构造 events，重新跑 bayesian_analysis + generate_html。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from update_dashboard import bayesian_analysis, generate_html

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(REPO_DIR, 'data.json')


def rebuild_events(matches, sport):
    events = []
    for m in matches:
        home = m.get('home', '')
        away = m.get('away', '')
        bookmakers = []
        for cn, o in (m.get('companies') or {}).items():
            oh = o.get('home', 0)
            od = o.get('draw', 0)
            oa = o.get('away', 0)
            if not (oh > 1 and oa > 1):
                continue
            outcomes = [
                {'name': home, 'price': oh},
                {'name': away, 'price': oa},
            ]
            if sport == 'football' and od and od > 1:
                outcomes.append({'name': 'Draw', 'price': od})
            bookmakers.append({
                'title': cn,
                'markets': [{'key': 'h2h', 'outcomes': outcomes}],
            })
        # 若没有分公司明细，用中位数赔率构造一家“综合”公司
        if not bookmakers:
            od = m.get('odds', {})
            oh, oa = od.get('home', 0), od.get('away', 0)
            if oh > 1 and oa > 1:
                outcomes = [
                    {'name': home, 'price': oh},
                    {'name': away, 'price': oa},
                ]
                if sport == 'football' and od.get('draw', 0) > 1:
                    outcomes.append({'name': 'Draw', 'price': od['draw']})
                bookmakers.append({
                    'title': '综合',
                    'markets': [{'key': 'h2h', 'outcomes': outcomes}],
                })
        if not bookmakers:
            continue
        events.append({
            'home_team': home,
            'away_team': away,
            'commence_time': m.get('commence', ''),
            'sport_key': m.get('sport_key', ''),
            'sport_title': m.get('league_en') or m.get('league', ''),
            'bookmakers': bookmakers,
        })
    return events


def main():
    with open(DATA_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)

    football_in = data.get('football', [])
    basketball_in = data.get('basketball', [])

    fb_events = rebuild_events(football_in, 'football')
    bb_events = rebuild_events(basketball_in, 'basketball')

    print(f'重建 events: 足球 {len(fb_events)} 场, 篮球 {len(bb_events)} 场')

    football_data = bayesian_analysis(fb_events, 'football', elo_data=None)
    basketball_data = bayesian_analysis(bb_events, 'basketball', elo_data=None)

    # 按开赛时间升序排序
    football_data.sort(key=lambda x: x.get('commence', ''))
    basketball_data.sort(key=lambda x: x.get('commence', ''))

    print(f'重算完成: 足球 {len(football_data)} 场, 篮球 {len(basketball_data)} 场')

    all_data = {
        'update_time': data.get('update_time', ''),
        'football': football_data,
        'basketball': basketball_data,
    }

    with open(DATA_PATH, 'w', encoding='utf-8') as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)
    print(f'data.json 已更新')

    html = generate_html(all_data)
    with open(os.path.join(REPO_DIR, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'index.html 已重新生成 ({len(html)//1024}KB)')


if __name__ == '__main__':
    main()
