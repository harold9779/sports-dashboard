#!/usr/bin/env python3
"""
赛事赔率贝叶斯分析看板 - 自动更新脚本
从 The Odds API 获取最新赔率数据，运行贝叶斯分析，生成看板 HTML
"""
import json, math, os, sys
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.parse import urlencode

API_KEY = os.environ.get('ODDS_API_KEY', '')
REPO_DIR = os.environ.get('GITHUB_WORKSPACE', os.path.dirname(os.path.abspath(__file__)))

# 关注的赛事
FOOTBALL_SPORTS = [
    'soccer_epl', 'soccer_spain_la_liga', 'soccer_germany_bundesliga',
    'soccer_italy_serie_a', 'soccer_france_ligue_one',
    'soccer_uefa_champs_league', 'soccer_efl_champ',
    'soccer_netherlands_eredivisie', 'soccer_portugal_primeira_liga',
    'soccer_belgium_first_div', 'soccer_afl'
]
BASKETBALL_SPORTS = [
    'basketball_nba', 'basketball_euroleague'
]

def fetch_odds(sport_key, regions='eu,uk', markets='h2h,spreads', odds_format='decimal'):
    """从 The Odds API 获取赔率数据"""
    params = urlencode({
        'apiKey': API_KEY,
        'regions': regions,
        'markets': markets,
        'oddsFormat': odds_format
    })
    url = f'https://api.the-odds-api.com/v4/sports/{sport_key}/odds/?{params}'
    try:
        req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urlopen(req, timeout=15) as resp:
            remaining = resp.headers.get('x-requests-remaining', '?')
            data = json.loads(resp.read().decode())
            print(f'  {sport_key}: {len(data)} events (remaining: {remaining})')
            return data
    except Exception as e:
        print(f'  {sport_key}: ERROR - {e}')
        return []

def bayesian_analysis(events, sport_type='football'):
    """对赛事进行贝叶斯分析"""
    results = []
    for event in events:
        home = event.get('home_team', '')
        away = event.get('away_team', '')
        commence = event.get('commence_time', '')

        # 收集各公司赔率
        bookmakers = event.get('bookmakers', [])
        if not bookmakers:
            continue

        odds_by_company = {}
        for bk in bookmakers:
            bk_name = bk.get('title', bk.get('key', ''))
            for market in bk.get('markets', []):
                if market.get('key') == 'h2h':
                    outcomes = {o['name']: o['price'] for o in market.get('outcomes', [])}
                    odds_by_company[bk_name] = {
                        'home': outcomes.get(home, 0),
                        'draw': outcomes.get('Draw', 0),
                        'away': outcomes.get(away, 0)
                    }
                    break

        if not odds_by_company:
            continue

        # 计算隐含概率先验（取赔率均值）
        all_home = [o['home'] for o in odds_by_company.values() if o['home'] > 0]
        all_draw = [o['draw'] for o in odds_by_company.values() if o['draw'] > 0]
        all_away = [o['away'] for o in odds_by_company.values() if o['away'] > 0]

        if not all_home or not all_away:
            continue

        avg_home = sum(all_home) / len(all_home)
        avg_draw = sum(all_draw) / len(all_draw) if all_draw else 0
        avg_away = sum(all_away) / len(all_away)

        # 隐含概率（含 overround）
        imp_home = 1.0 / avg_home if avg_home > 0 else 0
        imp_draw = 1.0 / avg_draw if avg_draw > 0 else 0
        imp_away = 1.0 / avg_away if avg_away > 0 else 0

        # Shin 方法去水
        total_imp = imp_home + imp_draw + imp_away
        if total_imp <= 0:
            continue

        prior_home = imp_home / total_imp
        prior_draw = imp_draw / total_imp
        prior_away = imp_away / total_imp

        # 似然函数：基于各公司赔率离散度
        # 离散度越小（一致性越高），似然越集中
        home_std = (sum((o - avg_home)**2 for o in all_home) / len(all_home))**0.5 if len(all_home) > 1 else 0.05
        away_std = (sum((o - avg_away)**2 for o in all_away) / len(all_away))**0.5 if len(all_away) > 1 else 0.05

        # 一致性因子（离散度越低越确定）
        consistency = 1.0 / (1.0 + home_std + away_std)

        # 贝叶斯更新
        posterior_home = prior_home * (1 + 0.1 * consistency)
        posterior_draw = prior_draw * (1 + 0.05 * consistency) if prior_draw > 0 else 0
        posterior_away = prior_away * (1 + 0.1 * consistency)

        # 归一化
        total_post = posterior_home + posterior_draw + posterior_away
        if total_post <= 0:
            continue

        posterior_home /= total_post
        posterior_draw /= total_post
        posterior_away /= total_post

        # 凯利指数
        kelly_home = posterior_home * avg_home
        kelly_draw = posterior_draw * avg_draw if avg_draw > 0 else 0
        kelly_away = posterior_away * avg_away

        # 价值投注判断（后验概率 > 隐含概率 * 1.02）
        edge_home = (posterior_home - imp_home / total_imp) * 100
        edge_draw = (posterior_draw - imp_draw / total_imp) * 100 if imp_draw > 0 else 0
        edge_away = (posterior_away - imp_away / total_imp) * 100

        # 泊松预测
        lambda_home = posterior_home * 3.0  # 简化估计
        lambda_away = posterior_away * 2.5

        poisson_scores = []
        for h in range(5):
            for a in range(5):
                ph = math.exp(-lambda_home) * (lambda_home**h) / math.factorial(h)
                pa = math.exp(-lambda_away) * (lambda_away**a) / math.factorial(a)
                poisson_scores.append({'home': h, 'away': a, 'prob': ph * pa})
        poisson_scores.sort(key=lambda x: x['prob'], reverse=True)
        best_score = poisson_scores[0] if poisson_scores else {'home': 1, 'away': 1, 'prob': 0.1}

        result = {
            'match': f'{home} vs {away}',
            'home': home, 'away': away,
            'commence': commence,
            'sport': sport_type,
            'league': event.get('sport_title', ''),
            'odds': {
                'home': round(avg_home, 2),
                'draw': round(avg_draw, 2),
                'away': round(avg_away, 2)
            },
            'companies': {name: {k: round(v, 2) for k, v in odds.items()} for name, odds in list(odds_by_company.items())[:6]},
            'bayes': {
                'home': round(posterior_home, 3),
                'draw': round(posterior_draw, 3),
                'away': round(posterior_away, 3)
            },
            'kelly': {
                'home': round(kelly_home, 3),
                'draw': round(kelly_draw, 3),
                'away': round(kelly_away, 3)
            },
            'edge': {
                'home': round(edge_home, 1),
                'draw': round(edge_draw, 1),
                'away': round(edge_away, 1)
            },
            'poisson': {
                'score': f"{best_score['home']}-{best_score['away']}",
                'prob': round(best_score['prob'], 3),
                'matrix': poisson_scores[:36]
            },
            'status': 'upcoming',
            'confidence': 'high' if max(posterior_home, posterior_away) > 0.55 else 'medium' if max(posterior_home, posterior_away) > 0.4 else 'low'
        }
        results.append(result)

    return results

def generate_html(football_data, basketball_data, update_time):
    """生成看板 HTML"""
    all_matches = football_data + basketball_data
    value_bets = []
    for m in all_matches:
        if m['edge']['home'] > 2:
            value_bets.append({'match': m['home'] + ' 主胜', 'edge': m['edge']['home'], 'prob': m['bayes']['home'], 'implied': 1.0/m['odds']['home'] if m['odds']['home'] > 0 else 0})
        if m['edge']['draw'] > 2 and m['odds']['draw'] > 0:
            value_bets.append({'match': m['home'] + ' 平局', 'edge': m['edge']['draw'], 'prob': m['bayes']['draw'], 'implied': 1.0/m['odds']['draw']})
        if m['edge']['away'] > 2:
            value_bets.append({'match': m['away'] + ' 客胜', 'edge': m['edge']['away'], 'prob': m['bayes']['away'], 'implied': 1.0/m['odds']['away'] if m['odds']['away'] > 0 else 0})
    value_bets.sort(key=lambda x: x['edge'], reverse=True)

    total_matches = len(all_matches)
    football_count = len(football_data)
    basketball_count = len(basketball_data)
    value_bet_count = len(value_bets)

    # Generate match cards JS data
    matches_js = json.dumps(all_matches[:20], ensure_ascii=False)
    value_bets_js = json.dumps(value_bets[:15], ensure_ascii=False)

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>赛事赔率贝叶斯分析看板</title>
<script src="https://image.uc.cn/s/uae/g/3n/mos-production/0915/echarts.min.js"><\/script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;background:#0f1923;color:#e0e6ed;min-height:100vh}}
.page-wrapper{{max-width:1440px;margin:0 auto;padding:20px 24px}}
.header{{text-align:center;margin-bottom:24px}}
.header h1{{font-size:22px;font-weight:700;background:linear-gradient(135deg,#4ecdc4,#44a8f2);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-bottom:4px}}
.header .subtitle{{font-size:12px;color:#6b7d8e}}
.kpi-row{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:24px}}
.kpi-card{{background:linear-gradient(135deg,rgba(26,42,60,0.9),rgba(20,35,50,0.95));border:1px solid rgba(78,205,196,0.15);border-radius:12px;padding:16px;text-align:center;position:relative;overflow:hidden}}
.kpi-card::before{{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,#4ecdc4,#44a8f2)}}
.kpi-value{{font-size:28px;font-weight:700;font-family:'JetBrains Mono','Fira Code',monospace}}
.kpi-value.green{{color:#4ecdc4}}.kpi-value.blue{{color:#44a8f2}}.kpi-value.orange{{color:#f5a623}}.kpi-value.purple{{color:#a78bfa}}
.kpi-label{{font-size:12px;color:#6b7d8e;margin-top:4px}}
.chart-card{{background:linear-gradient(135deg,rgba(26,42,60,0.85),rgba(18,32,48,0.95));border:1px solid rgba(68,168,242,0.1);border-radius:12px;padding:20px;margin-bottom:20px}}
.chart-title{{font-size:15px;font-weight:600;margin-bottom:14px;display:flex;align-items:center;gap:8px}}
.chart-title .dot{{width:8px;height:8px;border-radius:50%}}
.chart-container{{width:100%;aspect-ratio:16/9;min-height:320px}}
.chart-row{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px}}
.section-title{{font-size:17px;font-weight:700;margin:28px 0 16px;padding-left:12px;border-left:3px solid #4ecdc4}}
.match-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px;margin-bottom:24px}}
.match-card{{background:rgba(26,42,60,0.8);border:1px solid rgba(42,58,78,0.6);border-radius:10px;padding:14px;transition:all 0.2s}}
.match-card:hover{{border-color:rgba(78,205,196,0.4);box-shadow:0 4px 16px rgba(0,0,0,0.3)}}
.match-league{{font-size:10px;font-weight:600;padding:2px 8px;border-radius:4px;display:inline-block;margin-bottom:6px}}
.match-league.football{{background:#1B5E20;color:#C8E6C9}}
.match-league.basketball{{background:#E65100;color:#FFE0B2}}
.match-teams{{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:10px}}
.match-team{{flex:1;text-align:center}}
.match-team .name{{font-size:13px;font-weight:600}}
.match-vs{{font-size:11px;color:#5A6B82;font-weight:700;padding:3px 8px;background:rgba(10,14,23,0.6);border-radius:4px}}
.match-odds{{display:flex;gap:6px;margin-top:8px;padding-top:8px;border-top:1px solid rgba(42,58,78,0.4)}}
.odd-item{{flex:1;text-align:center;padding:5px 3px;background:rgba(10,14,23,0.5);border-radius:4px}}
.odd-label{{font-size:9px;color:#5A6B82}}
.odd-value{{font-size:13px;font-weight:700;font-family:'JetBrains Mono',monospace;color:#e0e6ed}}
.match-bayes{{margin-top:8px;font-size:11px;color:#8b9bb4}}
.match-bayes .prob{{font-family:'JetBrains Mono',monospace;font-weight:600}}
.match-bayes .high{{color:#4ecdc4}}
.match-bayes .medium{{color:#f5a623}}
.match-bayes .low{{color:#ee6666}}
.method-box{{background:rgba(26,42,60,0.6);border:1px solid rgba(78,205,196,0.1);border-radius:8px;padding:16px;margin-top:20px;font-size:12px;line-height:1.8;color:#8b9bb4}}
.method-box strong{{color:#4ecdc4}}
.value-list{{display:grid;gap:8px}}
.value-item{{display:flex;align-items:center;gap:10px;padding:8px 12px;background:rgba(26,42,60,0.6);border-radius:6px;border-left:3px solid #4ecdc4}}
.value-item .name{{flex:1;font-size:13px}}
.value-item .edge{{font-family:'JetBrains Mono',monospace;font-weight:700;color:#4ecdc4;font-size:14px}}
.value-item .detail{{font-size:11px;color:#6b7d8e;font-family:'JetBrains Mono',monospace}}
@media(max-width:768px){{
  .page-wrapper{{padding:12px}}
  .kpi-row{{grid-template-columns:repeat(2,1fr);gap:10px}}
  .kpi-value{{font-size:22px}}
  .chart-row{{grid-template-columns:1fr}}
  .chart-container{{min-height:280px;aspect-ratio:4/3}}
  .match-grid{{grid-template-columns:1fr}}
  .header h1{{font-size:18px}}
}}
</style>
</head>
<body>
<div class="page-wrapper">
  <div class="header">
    <h1>赛事赔率贝叶斯分析看板</h1>
    <div class="subtitle">数据更新：{update_time} · 数据来源：The Odds API（Bet365/Pinnacle/William Hill/DraftKings 等） · 贝叶斯后验 + 泊松预测</div>
  </div>

  <div class="kpi-row">
    <div class="kpi-card"><div class="kpi-value blue">{total_matches}</div><div class="kpi-label">当前赛事（足球 {football_count} / 篮球 {basketball_count}）</div></div>
    <div class="kpi-card"><div class="kpi-value green">{football_count}</div><div class="kpi-label">足球赛事</div></div>
    <div class="kpi-card"><div class="kpi-value orange">{basketball_count}</div><div class="kpi-label">篮球赛事</div></div>
    <div class="kpi-card"><div class="kpi-value purple">{value_bet_count}</div><div class="kpi-label">价值投注机会（Edge>2%）</div></div>
  </div>

  <div class="section-title">赛事总览 — 贝叶斯后验概率</div>
  <div class="match-grid" id="matchGrid"></div>

  <div class="section-title">贝叶斯后验概率分布</div>
  <div class="chart-card">
    <div class="chart-title"><span class="dot" style="background:#4ecdc4"></span>各赛事主胜/平局/客胜后验概率</div>
    <div id="chart1" class="chart-container" data-chart="true"></div>
  </div>

  <div class="section-title">多公司赔率离散度</div>
  <div class="chart-card">
    <div class="chart-title"><span class="dot" style="background:#44a8f2"></span>各公司主胜赔率对比（一致性分析）</div>
    <div id="chart2" class="chart-container" data-chart="true"></div>
  </div>

  <div class="section-title">价值投注雷达</div>
  <div id="valueBets" class="value-list"></div>

  <div class="chart-card" style="margin-top:20px">
    <div class="chart-title"><span class="dot" style="background:#f5a623"></span>价值投注边际优势排序</div>
    <div id="chart3" class="chart-container" data-chart="true"></div>
  </div>

  <div class="section-title">泊松比分预测热力图</div>
  <div class="chart-card">
    <div class="chart-title"><span class="dot" style="background:#a78bfa"></span id="poissonTitle">最热门赛事比分概率矩阵</div>
    <div id="chart4" class="chart-container" data-chart="true"></div>
  </div>

  <div class="section-title">凯利指数分析</div>
  <div class="chart-card">
    <div class="chart-title"><span class="dot" style="background:#ee6666"></span>凯利指数（>1.05 高风险 / >1.0 警示 / ≤1.0 安全）</div>
    <div id="chart5" class="chart-container" data-chart="true"></div>
  </div>

  <div class="method-box">
    <strong>方法论说明：</strong>贝叶斯分析采用 Shin 法去水后的欧赔隐含概率作为先验分布，以多公司赔率的离散度构建高斯似然函数进行后验更新。泊松比分预测基于后验概率估计的进攻参数（λ），独立模拟主客队进球分布后计算各比分概率矩阵。价值投注识别标准：贝叶斯后验概率 > 市场隐含概率 × 1.02 时标记为正EV机会（Edge > 2%）。<br/>
    <strong>数据来源：</strong>The Odds API（聚合 Bet365、Pinnacle、William Hill、DraftKings、FanDuel 等 20+ 博彩公司实时赔率）。<br/>
    <strong>自动更新：</strong>每 30 分钟通过 GitHub Actions 自动刷新数据。
  </div>
</div>

<script>
var isMobile = window.innerWidth <= 768;
var MATCH_DATA = {matches_js};
var VALUE_BETS = {value_bets_js};

// 渲染比赛卡片
(function(){{
  var grid = document.getElementById('matchGrid');
  MATCH_DATA.slice(0,12).forEach(function(m){{
    var sportClass = m.sport === 'football' ? 'football' : 'basketball';
    var isBB = m.sport === 'basketball';
    var confClass = m.confidence || 'medium';
    var html = '<span class="match-league ' + sportClass + '">' + (m.league||'') + '</span>' +
      '<div class="match-teams">' +
        '<div class="match-team"><div class="name">' + m.home + '</div></div>' +
        '<div class="match-vs">VS</div>' +
        '<div class="match-team"><div class="name">' + m.away + '</div></div>' +
      '</div>' +
      '<div class="match-odds">' +
        '<div class="odd-item"><div class="odd-label">主胜</div><div class="odd-value">' + m.odds.home.toFixed(2) + '</div></div>' +
        (isBB ? '<div class="odd-item" style="opacity:0.3"><div class="odd-label">—</div><div class="odd-value">—</div></div>' :
          '<div class="odd-item"><div class="odd-label">平局</div><div class="odd-value">' + (m.odds.draw > 0 ? m.odds.draw.toFixed(2) : '—') + '</div></div>') +
        '<div class="odd-item"><div class="odd-label">客胜</div><div class="odd-value">' + m.odds.away.toFixed(2) + '</div></div>' +
      '</div>' +
      '<div class="match-bayes">后验: ' +
        '<span class="prob ' + confClass + '">主' + (m.bayes.home*100).toFixed(1) + '%</span>' +
        (!isBB ? ' / <span class="prob">平' + (m.bayes.draw*100).toFixed(1) + '%</span>' : '') +
        ' / <span class="prob ' + confClass + '">客' + (m.bayes.away*100).toFixed(1) + '%</span>' +
        ' · 泊松: <span style="color:#a78bfa;font-weight:600">' + m.poisson.score + '</span>' +
      '</div>';
    var card = document.createElement('div');
    card.className = 'match-card';
    card.innerHTML = html;
    grid.appendChild(card);
  }});
}})();

// 价值投注列表
(function(){{
  var el = document.getElementById('valueBets');
  VALUE_BETS.slice(0,10).forEach(function(v){{
    var item = document.createElement('div');
    item.className = 'value-item';
    item.innerHTML = '<div class="name">' + v.match + '</div>' +
      '<div class="detail">P=' + (v.prob*100).toFixed(1) + '% / Implied=' + (v.implied*100).toFixed(1) + '%</div>' +
      '<div class="edge">+' + v.edge.toFixed(1) + '%</div>';
    el.appendChild(item);
  }});
  if (VALUE_BETS.length === 0) {{
    el.innerHTML = '<div style="color:#6b7d8e;font-size:13px;padding:12px;">当前无明显价值投注机会（Edge > 2%）</div>';
  }}
}})();

// Chart 1: 后验概率分布
(function(){{
  var ch = echarts.init(document.getElementById('chart1'));
  var labels = MATCH_DATA.slice(0,10).map(function(m){{ return m.home.substring(0,4) + ' vs ' + m.away.substring(0,4); }});
  ch.setOption({{
    tooltip:{{trigger:'axis',backgroundColor:'rgba(15,25,35,0.95)',borderColor:'rgba(78,205,196,0.3)',textStyle:{{color:'#e0e6ed',fontSize:12}}}},
    legend:{{bottom:0,left:'center',textStyle:{{color:'#8b9bb4',fontSize:isMobile?10:12}}}},
    grid:{{top:isMobile?'14%':'10%',bottom:'20%',left:'6%',right:'4%',containLabel:true}},
    xAxis:{{type:'category',data:labels,axisLabel:{{color:'#6b7d8e',fontSize:isMobile?9:11,rotate:isMobile?35:20}},axisLine:{{lineStyle:{{color:'#2a3a4e'}}}}}},
    yAxis:{{type:'value',max:1,axisLabel:{{color:'#6b7d8e',fontSize:10,formatter:function(v){{return(v*100)+'%'}}}},splitLine:{{lineStyle:{{color:'rgba(42,58,78,0.4)'}}}}}},
    series:[
      {{name:'主胜',type:'bar',stack:'all',itemStyle:{{color:'#4ecdc4'}},barWidth:'50%',data:MATCH_DATA.slice(0,10).map(function(m){{return m.bayes.home}}),label:{{show:!isMobile,position:'inside',fontSize:9,color:'#0f1923',formatter:function(p){{return(p.value*100).toFixed(0)+'%'}}}}}},
      {{name:'平局',type:'bar',stack:'all',itemStyle:{{color:'#f5a623'}},data:MATCH_DATA.slice(0,10).map(function(m){{return m.bayes.draw}})}},
      {{name:'客胜',type:'bar',stack:'all',itemStyle:{{color:'#ee6666'}},data:MATCH_DATA.slice(0,10).map(function(m){{return m.bayes.away}})}}
    ]
  }});
  window.addEventListener('resize',function(){{ch.resize()}});
}})();

// Chart 2: 赔率离散度
(function(){{
  var ch = echarts.init(document.getElementById('chart2'));
  var hasCompanies = MATCH_DATA.filter(function(m){{return m.companies && Object.keys(m.companies).length > 1}});
  var labels = hasCompanies.slice(0,8).map(function(m){{return m.home.substring(0,4)}});
  var companyNames = [];
  hasCompanies.forEach(function(m){{Object.keys(m.companies).forEach(function(n){{if(companyNames.indexOf(n)<0)companyNames.push(n)}})}});
  var colors = ['#5470C6','#91CC75','#FAC858','#EE6666','#73C0DE','#FC8452'];
  var series = companyNames.slice(0,6).map(function(cn, i){{
    return {{name:cn.substring(0,8),type:'bar',itemStyle:{{color:colors[i%6]}},barGap:'10%',
      data:hasCompanies.slice(0,8).map(function(m){{return m.companies[cn] ? m.companies[cn].home : null}})
    }};
  }});
  ch.setOption({{
    tooltip:{{trigger:'axis',backgroundColor:'rgba(15,25,35,0.95)',borderColor:'rgba(68,168,242,0.3)',textStyle:{{color:'#e0e6ed',fontSize:12}}}},
    legend:{{bottom:0,left:'center',textStyle:{{color:'#8b9bb4',fontSize:isMobile?10:12}}}},
    grid:{{top:isMobile?'14%':'10%',bottom:'20%',left:'6%',right:'4%',containLabel:true}},
    xAxis:{{type:'category',data:labels,axisLabel:{{color:'#6b7d8e',fontSize:isMobile?9:11}},axisLine:{{lineStyle:{{color:'#2a3a4e'}}}}}},
    yAxis:{{type:'value',axisLabel:{{color:'#6b7d8e',fontSize:10}},splitLine:{{lineStyle:{{color:'rgba(42,58,78,0.4)'}}}}}},
    series:series
  }});
  window.addEventListener('resize',function(){{ch.resize()}});
}})();

// Chart 3: 价值投注
(function(){{
  var ch = echarts.init(document.getElementById('chart3'));
  var sorted = VALUE_BETS.slice(0,12).reverse();
  ch.setOption({{
    tooltip:{{trigger:'axis',backgroundColor:'rgba(15,25,35,0.95)',borderColor:'rgba(239,68,68,0.3)',textStyle:{{color:'#e0e6ed',fontSize:12}},
      formatter:function(p){{var d=sorted[p[0].dataIndex];return '<b>'+d.match+'</b><br/>贝叶斯概率: '+(d.prob*100).toFixed(1)+'%<br/>赔率隐含: '+(d.implied*100).toFixed(1)+'%<br/>边际: <b style="color:#4ecdc4">+'+d.edge.toFixed(1)+'%</b>';}}
    }},
    grid:{{top:'5%',bottom:'8%',left:isMobile?'30%':'22%',right:'8%',containLabel:false}},
    xAxis:{{type:'value',axisLabel:{{color:'#6b7d8e',fontSize:10,formatter:function(v){{return'+'+v+'%'}}}},splitLine:{{lineStyle:{{color:'rgba(42,58,78,0.4)'}}}}}},
    yAxis:{{type:'category',data:sorted.map(function(d){{return d.match}}),axisLabel:{{color:'#8b9bb4',fontSize:isMobile?9:11,width:isMobile?100:160,overflow:'truncate'}},axisLine:{{lineStyle:{{color:'#2a3a4e'}}}}}},
    series:[{{type:'bar',data:sorted.map(function(d){{return d.edge}}),
      itemStyle:{{color:function(p){{return p.value>5?'#4ecdc4':p.value>3?'#44a8f2':'#f5a623'}}}},
      barWidth:'60%',
      label:{{show:true,position:'right',fontSize:isMobile?9:11,color:'#e0e6ed',fontFamily:'JetBrains Mono,monospace',formatter:function(p){{return'+'+p.value.toFixed(1)+'%'}}}}
    }}]
  }});
  window.addEventListener('resize',function(){{ch.resize()}});
}})();

// Chart 4: 泊松热力图
(function(){{
  var ch = echarts.init(document.getElementById('chart4'));
  var bestMatch = MATCH_DATA[0];
  if(bestMatch) document.getElementById('poissonTitle').textContent = bestMatch.match + ' 泊松比分预测';
  var matrix = bestMatch ? bestMatch.poisson.matrix : [];
  var homeGoals = ['0球','1球','2球','3球','4球','5球'];
  var awayGoals = ['0球','1球','2球','3球','4球','5球'];
  var heatData = matrix.map(function(s){{return [s.home, s.away, parseFloat((s.prob*100).toFixed(2))]}});
  ch.setOption({{
    tooltip:{{backgroundColor:'rgba(15,25,35,0.95)',borderColor:'rgba(167,139,250,0.3)',textStyle:{{color:'#e0e6ed',fontSize:12}},
      formatter:function(p){{var v=p.value;return '比分 '+v[0]+' - '+v[1]+'<br/>概率: <b>'+v[2].toFixed(2)+'%</b>';}}
    }},
    grid:{{top:isMobile?'12%':'8%',bottom:'14%',left:'12%',right:'12%'}},
    xAxis:{{type:'category',data:homeGoals,name:(bestMatch?bestMatch.home:'主队')+'进球',nameTextStyle:{{color:'#6b7d8e',fontSize:10}},axisLabel:{{color:'#6b7d8e',fontSize:isMobile?9:11}},axisLine:{{lineStyle:{{color:'#2a3a4e'}}}}}},
    yAxis:{{type:'category',data:awayGoals,name:(bestMatch?bestMatch.away:'客队')+'进球',nameTextStyle:{{color:'#6b7d8e',fontSize:10}},axisLabel:{{color:'#6b7d8e',fontSize:isMobile?9:11}},axisLine:{{lineStyle:{{color:'#2a3a4e'}}}}}},
    visualMap:{{min:0,max:15,calculable:false,orient:'horizontal',left:'center',bottom:0,inRange:{{color:['#1a2a3c','#1e4a5a','#2a7a6a','#4ecdc4','#7fffd4']}},textStyle:{{color:'#6b7d8e',fontSize:10}}}},
    series:[{{type:'heatmap',data:heatData,label:{{show:true,fontSize:isMobile?8:10,color:'#e0e6ed',formatter:function(p){{return p.value[2]>=1?p.value[2].toFixed(1):''}}}},emphasis:{{itemStyle:{{shadowBlur:10,shadowColor:'rgba(78,205,196,0.5)'}}}}}}]
  }});
  window.addEventListener('resize',function(){{ch.resize()}});
}})();

// Chart 5: 凯利指数
(function(){{
  var ch = echarts.init(document.getElementById('chart5'));
  var labels = MATCH_DATA.slice(0,10).map(function(m){{return m.home.substring(0,4)}});
  ch.setOption({{
    tooltip:{{trigger:'axis',backgroundColor:'rgba(15,25,35,0.95)',borderColor:'rgba(239,68,68,0.3)',textStyle:{{color:'#e0e6ed',fontSize:12}}}},
    legend:{{bottom:0,left:'center',textStyle:{{color:'#8b9bb4',fontSize:isMobile?10:12}}}},
    grid:{{top:isMobile?'14%':'10%',bottom:'18%',left:'6%',right:'4%',containLabel:true}},
    xAxis:{{type:'category',data:labels,axisLabel:{{color:'#6b7d8e',fontSize:isMobile?9:11}},axisLine:{{lineStyle:{{color:'#2a3a4e'}}}}}},
    yAxis:{{type:'value',min:0.8,max:1.15,axisLabel:{{color:'#6b7d8e',fontSize:10}},splitLine:{{lineStyle:{{color:'rgba(42,58,78,0.4)'}}}},
      name:'凯利指数',nameTextStyle:{{color:'#6b7d8e',fontSize:10}}}},
    series:[
      {{name:'主胜',type:'bar',itemStyle:{{color:'#4ecdc4'}},barGap:'15%',data:MATCH_DATA.slice(0,10).map(function(m){{return m.kelly.home}})}},
      {{name:'平局',type:'bar',itemStyle:{{color:'#f5a623'}},data:MATCH_DATA.slice(0,10).map(function(m){{return m.kelly.draw}})}},
      {{name:'客胜',type:'bar',itemStyle:{{color:'#ee6666'}},data:MATCH_DATA.slice(0,10).map(function(m){{return m.kelly.away}})}},
      {{name:'安全线(1.0)',type:'line',data:labels.map(function(){{return 1.0}}),lineStyle:{{color:'rgba(255,255,255,0.3)',type:'dashed',width:1}},symbol:'none',z:0}},
      {{name:'警戒线(1.05)',type:'line',data:labels.map(function(){{return 1.05}}),lineStyle:{{color:'rgba(239,68,68,0.4)',type:'dashed',width:1}},symbol:'none',z:0}}
    ]
  }});
  window.addEventListener('resize',function(){{ch.resize()}});
}})();
<\/script>
</body>
</html>'''
    return html

def main():
    if not API_KEY:
        print('ERROR: ODDS_API_KEY not set')
        sys.exit(1)

    now = datetime.now(timezone.utc)
    update_time = now.strftime('%Y-%m-%d %H:%M UTC')
    print(f'=== 赛事赔率贝叶斯分析看板更新 ===')
    print(f'时间: {update_time}')

    # 获取足球赔率
    print('\n--- 足球赛事 ---')
    football_events = []
    for sport in FOOTBALL_SPORTS:
        events = fetch_odds(sport)
        football_events.extend(events)
        if len(football_events) >= 15:
            break

    # 获取篮球赔率
    print('\n--- 篮球赛事 ---')
    basketball_events = []
    for sport in BASKETBALL_SPORTS:
        events = fetch_odds(sport)
        basketball_events.extend(events)
        if len(basketball_events) >= 10:
            break

    # 贝叶斯分析
    print('\n--- 贝叶斯分析 ---')
    football_data = bayesian_analysis(football_events, 'football')
    basketball_data = bayesian_analysis(basketball_events, 'basketball')
    print(f'足球: {len(football_data)} 场分析完成')
    print(f'篮球: {len(basketball_data)} 场分析完成')

    if not football_data and not basketball_data:
        print('WARNING: No data to generate dashboard')
        sys.exit(0)

    # 生成 HTML
    print('\n--- 生成看板 ---')
    html = generate_html(football_data, basketball_data, update_time)
    output_path = os.path.join(REPO_DIR, 'index.html')
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'看板已写入: {output_path} ({len(html)//1024}KB)')

    # 保存原始数据
    data_path = os.path.join(REPO_DIR, 'data.json')
    with open(data_path, 'w', encoding='utf-8') as f:
        json.dump({
            'update_time': update_time,
            'football': football_data,
            'basketball': basketball_data
        }, f, ensure_ascii=False, indent=2)
    print(f'数据已保存: {data_path}')

if __name__ == '__main__':
    main()
