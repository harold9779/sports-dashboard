#!/usr/bin/env python3
"""
赛事赔率贝叶斯分析看板 - 自动更新脚本 v3
修复: Edge计算、Kelly公式、篮球2路分支、时区、泊松Top-N、图表渲染
"""
import json, math, os, sys
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.parse import urlencode

API_KEY = os.environ.get('ODDS_API_KEY', '')
REPO_DIR = os.environ.get('GITHUB_WORKSPACE', os.path.dirname(os.path.abspath(__file__)))

FOOTBALL_SPORTS = [
    'soccer_epl', 'soccer_spain_la_liga', 'soccer_germany_bundesliga',
    'soccer_italy_serie_a', 'soccer_france_ligue_one',
    'soccer_uefa_champs_league', 'soccer_efl_champ',
]
BASKETBALL_SPORTS = ['basketball_nba', 'basketball_euroleague']

def fetch_odds(sport_key, regions='eu,uk', markets='h2h,spreads', odds_format='decimal'):
    params = urlencode({'apiKey': API_KEY, 'regions': regions, 'markets': markets, 'oddsFormat': odds_format})
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

# ========== Shin 去水法 ==========
def shin_dewater(odds_list):
    """
    Shin (1991) 方法去水: 从带水赔率还原真实概率
    odds_list: [odds_home, odds_draw, odds_away] 或 [odds_home, odds_away] (篮球2路)
    返回: 去水后的真实概率列表
    """
    n = len(odds_list)
    implied = [1.0 / o for o in odds_list]
    total = sum(implied)
    
    if n == 2:
        # 篮球2路: 简单按比例去水
        return [imp / total for imp in implied]
    
    # 足球3路: Shin方法
    # 求解 z (庄家利润率参数)
    # Shin模型: p_i = (1/odds_i) * (1 + z * sum_j(1/odds_j)) / (1 + z * n * sum_j(1/odds_j))
    # 简化: 用幂方法迭代求解
    z = (total - 1.0) / n  # 初始估计
    
    for _ in range(50):  # 迭代收敛
        probs = []
        for imp in implied:
            p = imp * (1.0 + z * total) / (1.0 + z * n * total)
            probs.append(p)
        
        s = sum(probs)
        if abs(s - 1.0) < 1e-8:
            break
        # 调整 z
        z = z * (1.0 / s)
    
    # 归一化
    s = sum(probs)
    return [p / s for p in probs]

# ========== 凯利公式 ==========
def kelly_fraction(p_model, odds):
    """
    凯利公式: f* = (p * b - q) / b
    其中 b = odds - 1 (净赔率), p = 模型概率, q = 1 - p
    返回: 凯利比例 (负数截断为0, 上限0.15风控)
    """
    if odds <= 1.0 or p_model <= 0:
        return 0.0
    b = odds - 1.0
    q = 1.0 - p_model
    f = (p_model * b - q) / b
    f = max(0.0, min(f, 0.15))  # 截断 [0, 0.15]
    return round(f, 4)

# ========== 贝叶斯分析 ==========
def bayesian_analysis(events, sport_type='football'):
    is_basketball = sport_type == 'basketball'
    results = []
    
    for event in events:
        home = event.get('home_team', '')
        away = event.get('away_team', '')
        commence = event.get('commence_time', '')
        bookmakers = event.get('bookmakers', [])
        if not bookmakers:
            continue
        
        # 收集各公司赔率
        odds_by_company = {}
        for bk in bookmakers:
            bk_name = bk.get('title', bk.get('key', ''))
            for market in bk.get('markets', []):
                if market.get('key') == 'h2h':
                    outcomes = {o['name']: o['price'] for o in market.get('outcomes', [])}
                    h = outcomes.get(home, 0)
                    a = outcomes.get(away, 0)
                    d = outcomes.get('Draw', 0) if not is_basketball else 0
                    if h > 1.0 and a > 1.0:
                        if not is_basketball and d <= 1.0:
                            continue
                        odds_by_company[bk_name] = {'home': h, 'draw': d, 'away': a}
                    break
        
        if not odds_by_company:
            continue
        
        # 取赔率中位数 (对异常值更鲁棒)
        all_home = sorted([o['home'] for o in odds_by_company.values() if o['home'] > 0])
        all_away = sorted([o['away'] for o in odds_by_company.values() if o['away'] > 0])
        all_draw = sorted([o['draw'] for o in odds_by_company.values() if o['draw'] > 0]) if not is_basketball else []
        
        if len(all_home) < 2 or len(all_away) < 2:
            continue
        
        def median(lst):
            n = len(lst)
            if n == 0: return 0
            if n % 2 == 0: return (lst[n//2-1] + lst[n//2]) / 2
            return lst[n//2]
        
        med_home = median(all_home)
        med_away = median(all_away)
        med_draw = median(all_draw) if all_draw else 0
        
        # Shin 去水 -> 市场真实概率
        if is_basketball:
            market_probs = shin_dewater([med_home, med_away])
            p_market_home, p_market_draw, p_market_away = market_probs[0], 0.0, market_probs[1]
        else:
            market_probs = shin_dewater([med_home, med_draw, med_away])
            p_market_home, p_market_draw, p_market_away = market_probs[0], market_probs[1], market_probs[2]
        
        # 贝叶斯更新: 用赔率离散度作为信号强度
        # 离散度越小 (各公司一致) -> 市场信号越强 -> 后验越接近市场
        home_std = (sum((o - med_home)**2 for o in all_home) / len(all_home))**0.5 if len(all_home) > 1 else 0.02
        away_std = (sum((o - med_away)**2 for o in all_away) / len(all_away))**0.5 if len(all_away) > 1 else 0.02
        
        # 信号强度: 离散度低 = 市场信息充分
        signal = 1.0 / (1.0 + (home_std + away_std) * 10)
        
        # 先验: 均匀分布 (无信息先验)
        if is_basketball:
            prior_home, prior_away = 0.5, 0.5
            # 后验 = 先验 * (1 - signal) + 市场 * signal
            post_home = prior_home * (1 - signal) + p_market_home * signal
            post_away = prior_away * (1 - signal) + p_market_away * signal
            post_draw = 0.0
            total = post_home + post_away
        else:
            prior_home, prior_draw, prior_away = 1/3, 1/3, 1/3
            post_home = prior_home * (1 - signal) + p_market_home * signal
            post_draw = prior_draw * (1 - signal) + p_market_draw * signal
            post_away = prior_away * (1 - signal) + p_market_away * signal
            total = post_home + post_draw + post_away
        
        if total <= 0:
            continue
        post_home /= total
        post_draw /= total
        post_away /= total
        
        # Edge = 贝叶斯后验 - 市场去水概率 (百分比)
        edge_home = (post_home - p_market_home) * 100
        edge_draw = (post_draw - p_market_draw) * 100 if not is_basketball else 0
        edge_away = (post_away - p_market_away) * 100
        
        # 凯利指数 (用中位数赔率 + 贝叶斯概率)
        k_home = kelly_fraction(post_home, med_home)
        k_draw = kelly_fraction(post_draw, med_draw) if not is_basketball else 0
        k_away = kelly_fraction(post_away, med_away)
        
        # 凯利比值 (posterior * odds, 用于快速风控展示)
        kelly_ratio_home = round(post_home * med_home, 3)
        kelly_ratio_draw = round(post_draw * med_draw, 3) if not is_basketball else 0
        kelly_ratio_away = round(post_away * med_away, 3)
        
        # 泊松比分预测 Top-5
        lambda_home = post_home * 3.2
        lambda_away = post_away * 2.4
        poisson_scores = []
        for h in range(6):
            for a in range(6):
                if is_basketball:
                    continue  # 篮球不用泊松比分
                ph = math.exp(-lambda_home) * (lambda_home**h) / math.factorial(h)
                pa = math.exp(-lambda_away) * (lambda_away**a) / math.factorial(a)
                poisson_scores.append({'home': h, 'away': a, 'prob': ph * pa})
        
        if is_basketball:
            poisson_scores = []
        
        poisson_scores.sort(key=lambda x: x['prob'], reverse=True)
        best = poisson_scores[0] if poisson_scores else {'home': 0, 'away': 0, 'prob': 0}
        top5 = poisson_scores[:5]
        
        result = {
            'match': f'{home} vs {away}', 'home': home, 'away': away,
            'commence': commence, 'sport': sport_type,
            'league': event.get('sport_title', ''),
            'odds': {'home': round(med_home, 2), 'draw': round(med_draw, 2), 'away': round(med_away, 2)},
            'companies': {n: {k: round(v, 2) for k, v in odds.items()} for n, odds in list(odds_by_company.items())[:6]},
            'market': {'home': round(p_market_home, 4), 'draw': round(p_market_draw, 4), 'away': round(p_market_away, 4)},
            'bayes': {'home': round(post_home, 4), 'draw': round(post_draw, 4), 'away': round(post_away, 4)},
            'edge': {'home': round(edge_home, 2), 'draw': round(edge_draw, 2), 'away': round(edge_away, 2)},
            'kelly': {'home': k_home, 'draw': k_draw, 'away': k_away},
            'kelly_ratio': {'home': kelly_ratio_home, 'draw': kelly_ratio_draw, 'away': kelly_ratio_away},
            'poisson': {
                'score': f"{best['home']}-{best['away']}" if poisson_scores else '',
                'prob': round(best['prob'], 4) if poisson_scores else 0,
                'top5': top5,
                'matrix': poisson_scores[:25]
            },
            'status': 'upcoming',
            'confidence': 'high' if max(post_home, post_away) > 0.55 else 'medium' if max(post_home, post_away) > 0.4 else 'low'
        }
        results.append(result)
    
    return results

def generate_html(data):
    data_json = json.dumps(data, ensure_ascii=False)
    # Read template
    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'template.html')
    if os.path.exists(template_path):
        with open(template_path, 'r', encoding='utf-8') as f:
            html = f.read()
        html = html.replace('__DATA_PLACEHOLDER__', data_json)
    else:
        print('WARNING: template.html not found, using inline fallback')
        html = get_inline_template(data_json)
        html = html.replace('__DATA_PLACEHOLDER__', data_json)
    return html

def get_inline_template(data_json):
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>赛事赔率贝叶斯分析看板</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<script>if(typeof echarts==='undefined'){{document.write('<scr'+'ipt src="https://cdnjs.cloudflare.com/ajax/libs/echarts/5.5.0/echarts.min.js"><\\/scr'+'ipt>')}}</script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:Tahoma,"Microsoft YaHei",sans-serif;background:#004b81;color:#333;font-size:12px}}
a{{color:#333;text-decoration:none}}a:hover{{color:#e62129}}
#tools{{width:100%;max-width:1200px;margin:0 auto;padding:6px 10px;background:#f6f6f6;border-bottom:1px solid #c0c0c0;display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:12px}}
#tools .btn{{display:inline-block;padding:2px 10px;line-height:22px;border:1px solid #c0c0c0;border-radius:2px;background:#fff;color:#333;cursor:pointer;font-size:12px}}
#tools .btn:hover{{border-color:#93c1d8;color:#228bd6}}
#tools .btn.on{{background:#FFEEB9;border-color:#DEA67C}}
#tools .txt{{color:#666;padding:0 4px}}
#tools .info{{margin-left:auto;color:#888;font-size:11px}}
.page{{max-width:1200px;margin:0 auto;background:#fff;min-height:100vh;box-shadow:0 0 20px rgba(0,0,0,.3)}}
.hdr{{background:linear-gradient(180deg,#1a6db5,#0d5a9e);padding:10px 16px;color:#fff;display:flex;justify-content:space-between;align-items:center;border-bottom:2px solid #004080}}
.hdr h1{{font-size:15px;font-weight:bold}}
.hdr .tm{{font-size:11px;color:#b8d4f0}}
.kpi{{display:grid;grid-template-columns:repeat(4,1fr);border-bottom:1px solid #d0d0d0}}
.kpi-cell{{text-align:center;padding:12px 8px;border-right:1px solid #e0e0e0;background:#f8fafc}}
.kpi-cell:last-child{{border-right:none}}
.kpi-cell .v{{font-size:24px;font-weight:bold;font-family:Tahoma,Arial}}
.kpi-cell .v.c1{{color:#1a6db5}}.kpi-cell .v.c2{{color:#2e7d32}}.kpi-cell .v.c3{{color:#e65100}}.kpi-cell .v.c4{{color:#c62828}}
.kpi-cell .l{{font-size:11px;color:#888;margin-top:3px}}
.sec-hdr{{background:linear-gradient(180deg,#e8f0f8,#d0dfe8);border-top:1px solid #b0c4d8;border-bottom:1px solid #b0c4d8;padding:7px 14px;font-size:13px;font-weight:bold;color:#1a4a7a}}
.tbl-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
.mtbl{{width:100%;border-collapse:collapse;font-size:12px;min-width:900px}}
.mtbl thead th{{background:#e0ecf5;padding:6px 8px;text-align:center;font-weight:bold;color:#1a4a7a;border-bottom:2px solid #1a6db5;font-size:11px;white-space:nowrap;position:sticky;top:0;z-index:1}}
.mtbl tbody tr{{border-bottom:1px solid #e8e8e8}}
.mtbl tbody tr:hover{{background:#f0f6ff}}
.mtbl tbody tr:nth-child(even){{background:#fafbfc}}
.mtbl td{{padding:7px 8px;text-align:center;vertical-align:middle;white-space:nowrap}}
.mtbl .lg{{display:inline-block;padding:1px 6px;border-radius:2px;font-size:10px;font-weight:bold;color:#fff}}
.mtbl .lg.fb{{background:#1B5E20}}.mtbl .lg.bb{{background:#E65100}}
.mtbl .tn{{font-weight:bold;color:#333}}
.mtbl .th{{text-align:right;padding-right:4px}}.mtbl .ta{{text-align:left;padding-left:4px}}
.mtbl .od{{font-family:Tahoma,Arial;font-weight:bold;font-size:12px}}
.mtbl .vs{{color:#999;font-weight:normal;font-size:11px}}
.mtbl .pbar{{display:inline-flex;height:10px;border-radius:2px;overflow:hidden;vertical-align:middle;margin-right:4px}}
.mtbl .pbar .h{{background:#2e7d32}}.mtbl .pbar .d{{background:#f5a623}}.mtbl .pbar .a{{background:#c62828}}
.mtbl .pt{{font-family:Tahoma,Arial;font-size:11px;font-weight:bold}}
.mtbl .pt-h{{color:#2e7d32}}.mtbl .pt-m{{color:#e65100}}.mtbl .pt-l{{color:#666}}
.mtbl .vb{{display:inline-block;padding:1px 5px;border-radius:2px;font-size:10px;font-weight:bold}}
.mtbl .vb.pos{{background:#e8f5e9;color:#2e7d32}}
.mtbl .kb{{display:inline-block;padding:1px 4px;border-radius:2px;font-size:10px;font-family:Tahoma;font-weight:bold}}
.mtbl .kb.safe{{background:#e8f5e9;color:#2e7d32}}.mtbl .kb.warn{{background:#fff3e0;color:#e65100}}.mtbl .kb.danger{{background:#ffebee;color:#c62828}}.mtbl .kb.zero{{background:#f5f5f5;color:#999}}
.vsec{{padding:10px 14px;border-bottom:1px solid #e0e0e0}}
.vlist{{display:flex;flex-wrap:wrap;gap:6px}}
.vitem{{display:flex;align-items:center;gap:6px;padding:5px 10px;background:#f0f9f0;border:1px solid #c8e6c9;border-radius:3px;font-size:12px}}
.vitem .nm{{font-weight:bold}}.vitem .eg{{font-family:Tahoma;font-weight:bold;color:#2e7d32;font-size:13px}}
.vitem .dt{{font-size:10px;color:#888;font-family:Tahoma}}
.chsec{{padding:14px;border-bottom:1px solid #e0e0e0}}
.chrow{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
.chbox{{background:#f8fafc;border:1px solid #d0dfe8;border-radius:4px;padding:10px}}
.chbox .cl{{font-size:12px;font-weight:bold;color:#1a4a7a;margin-bottom:6px;display:flex;align-items:center;gap:4px}}
.chbox .cl .dot{{width:7px;height:7px;border-radius:50%}}
.chbox .cc{{width:100%;height:280px}}
.ftr{{background:#f6f6f6;padding:10px 14px;font-size:11px;color:#888;line-height:1.7;border-top:1px solid #e0e0e0}}
.ftr b{{color:#1a6db5}}
.err-msg{{text-align:center;padding:30px;color:#c62828;font-size:14px;background:#fff3e0;border:1px solid #ffcc80;border-radius:4px;margin:10px}}
@media(max-width:768px){{
  .kpi{{grid-template-columns:repeat(2,1fr)}}
  .chrow{{grid-template-columns:1fr}}
  .chbox .cc{{height:220px}}
  .mtbl{{font-size:11px;min-width:700px}}
  .mtbl td{{padding:5px 4px}}
  .hdr h1{{font-size:13px}}
  #tools{{font-size:11px}}
  .vlist{{flex-direction:column}}
}}
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
  </div>
  <div class="sec-hdr">📋 赛事列表 — 贝叶斯后验概率 &amp; 赔率分析</div>
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
    <div class="chbox"><div class="cl"><span class="dot" style="background:#c62828"></span>凯利比值风控</div><div id="c6" class="cc"></div></div>
  </div></div>
  <div class="ftr">
    <b>方法论：</b>Shin法去水 → 市场隐含概率 → 赔率离散度加权贝叶斯更新 → 后验概率 → Edge = 后验 - 市场 → 凯利公式 f*=(pb-q)/b<br/>
    <b>数据来源：</b>The Odds API（Bet365 / Pinnacle / William Hill / DraftKings 等 20+ 博彩公司）· 每30分钟 GitHub Actions 自动刷新<br/>
    <b>时间说明：</b>开赛时间已转换为北京时间 (UTC+8)
  </div>
</div>
<script>
var D=__DATA_PLACEHOLDER__;
var MD=D.football.concat(D.basketball);
var VB=[];
var mob=window.innerWidth<=768;
var BJT=8*3600*1000;

// 过滤: 只保留未来7天内 + 未开赛的
var now=Date.now();
var cutoff=now+7*24*3600*1000;
MD=MD.filter(function(m){{
  if(!m.commence)return true;
  var t=new Date(m.commence).getTime();
  return t>now-3600000 && t<cutoff;
}});

if(MD.length===0){{
  document.getElementById('errMsg').innerHTML='<div class="err-msg">⚠️ 当前无可用赛事数据。可能原因：API 限流 / 密钥过期 / 暂无即将开赛的比赛。请稍后刷新或检查 GitHub Actions 运行日志。</div>';
}}

// 计算价值投注
MD.forEach(function(m){{
  var edges=m.edge;
  if(edges.home>2)VB.push({{n:m.home+' 主胜',e:edges.home,p:m.bayes.home,i:m.market.home,match:m.match,sport:m.sport}});
  if(edges.draw>2&&m.odds.draw>0)VB.push({{n:m.home+' 平局',e:edges.draw,p:m.bayes.draw,i:m.market.draw,match:m.match,sport:m.sport}});
  if(edges.away>2)VB.push({{n:m.away+' 客胜',e:edges.away,p:m.bayes.away,i:m.market.away,match:m.match,sport:m.sport}});
}});
VB.sort(function(a,b){{return b.e-a.e}});

document.getElementById('updateTime').textContent='更新: '+(D.update_time||'')+' (UTC) · 足球 '+D.football.length+' / 篮球 '+D.basketball.length;
document.getElementById('kTotal').textContent=MD.length;
document.getElementById('kFB').textContent=MD.filter(function(m){{return m.sport==='football'}}).length;
document.getElementById('kBB').textContent=MD.filter(function(m){{return m.sport==='basketball'}}).length;
document.getElementById('kVal').textContent=VB.length;

function toBJT(isoStr){{
  if(!isoStr)return '—';
  var d=new Date(isoStr);
  var bj=new Date(d.getTime()+BJT);
  return (bj.getMonth()+1)+'/'+bj.getDate()+' '+String(bj.getHours()).padStart(2,'0')+':'+String(bj.getMinutes()).padStart(2,'0');
}}

function renderTable(data){{
  var tb=document.getElementById('tbody');tb.innerHTML='';
  data.slice(0,30).forEach(function(m){{
    var bb=m.sport==='basketball',lg=bb?'bb':'fb',lt=m.league||'';
    var ts=toBJT(m.commence);
    
    // 贝叶斯后验显示
    var bh='';
    if(bb){{
      bh='<span class="pt pt-'+(m.bayes.home>.5?'h':'m')+'">主'+(m.bayes.home*100).toFixed(1)+'%</span>'
        +' / <span class="pt pt-'+(m.bayes.away>.5?'h':'m')+'">客'+(m.bayes.away*100).toFixed(1)+'%</span>';
    }}else{{
      bh='<span class="pt pt-'+(m.bayes.home>.5?'h':'m')+'">'+(m.bayes.home*100).toFixed(1)+'%</span>'
        +'<span class="pt" style="color:#999"> / '+(m.bayes.draw*100).toFixed(1)+'% / </span>'
        +'<span class="pt pt-'+(m.bayes.away>.5?'h':'m')+'">'+(m.bayes.away*100).toFixed(1)+'%</span>';
    }}
    
    // 概率条
    var bw=110,wh=Math.round(m.bayes.home*bw),wd=bb?0:Math.round(m.bayes.draw*bw),wa=bw-wh-wd;
    var pb='<div class="pbar" style="width:'+bw+'px"><div class="h" style="width:'+wh+'px"></div>'
      +(bb?'':'<div class="d" style="width:'+wd+'px"></div>')
      +'<div class="a" style="width:'+wa+'px"></div></div>';
    
    // 泊松Top3
    var ps='';
    if(m.poisson.top5&&m.poisson.top5.length>0){{
      ps=m.poisson.top5.slice(0,3).map(function(s){{
        return s.home+'-'+s.away+'('+( s.prob*100).toFixed(1)+'%)';
      }}).join(' ');
    }}else if(bb){{
      ps='<span style="color:#999">—</span>';
    }}
    
    // 凯利 f*
    var kf=m.kelly;
    function kcls(v){{return v>0.05?'danger':v>0.02?'warn':v>0?'safe':'zero'}}
    var kh='';
    if(bb){{
      kh='<span class="kb '+kcls(kf.home)+'">'+kf.home.toFixed(3)+'</span> / '
        +'<span class="kb '+kcls(kf.away)+'">'+kf.away.toFixed(3)+'</span>';
    }}else{{
      kh='<span class="kb '+kcls(kf.home)+'">'+kf.home.toFixed(3)+'</span> '
        +'<span class="kb '+kcls(kf.draw)+'">'+kf.draw.toFixed(3)+'</span> '
        +'<span class="kb '+kcls(kf.away)+'">'+kf.away.toFixed(3)+'</span>';
    }}
    
    // Edge
    var edges=m.edge;
    var maxE=Math.max(edges.home,edges.draw||0,edges.away);
    var vl='';
    if(maxE>2){{
      vl='<span class="vb pos">+'+maxE.toFixed(1)+'%</span>';
    }}else if(maxE>0){{
      vl='<span style="color:#999;font-size:10px">+'+maxE.toFixed(1)+'%</span>';
    }}else{{
      vl='<span style="color:#ccc">—</span>';
    }}
    
    // 平局列 (篮球不显示)
    var drawCell=bb?'<span style="color:#ccc">—</span>':'<span class="od">'+m.odds.draw.toFixed(2)+'</span>';
    
    var tr=document.createElement('tr');
    tr.innerHTML='<td><span class="lg '+lg+'">'+lt+'</span></td>'
      +'<td style="font-family:Tahoma;font-size:11px;color:#666">'+ts+'</td>'
      +'<td class="tn th">'+m.home+'</td><td class="vs">vs</td><td class="tn ta">'+m.away+'</td>'
      +'<td class="od">'+m.odds.home.toFixed(2)+'</td>'
      +'<td>'+drawCell+'</td>'
      +'<td class="od">'+m.odds.away.toFixed(2)+'</td>'
      +'<td>'+pb+'<br/>'+bh+'</td>'
      +'<td style="font-size:10px;color:#7b1fa2;font-family:Tahoma">'+ps+'</td>'
      +'<td style="font-size:11px">'+kh+'</td>'
      +'<td>'+vl+'</td>';
    tb.appendChild(tr);
  }});
}}

function renderVB(){{
  var el=document.getElementById('vlist');el.innerHTML='';
  if(!VB.length){{el.innerHTML='<div style="color:#999;padding:8px">当前无明显价值投注机会 (Edge &gt; 2%)。贝叶斯后验与市场概率高度一致，说明市场定价效率较高。</div>';return}}
  VB.slice(0,15).forEach(function(v){{
    var d=document.createElement('div');d.className='vitem';
    d.innerHTML='<span class="nm">'+v.n+'</span><span class="eg">+'+v.e.toFixed(1)+'%</span><span class="dt">贝叶斯='+(v.p*100).toFixed(1)+'% / 市场='+(v.i*100).toFixed(1)+'%</span>';
    el.appendChild(d);
  }});
}}

function showAll(){{renderTable(MD);hlBtn(0)}}
function showFB(){{renderTable(MD.filter(function(m){{return m.sport==='football'}}));hlBtn(1)}}
function showBB(){{renderTable(MD.filter(function(m){{return m.sport==='basketball'}}));hlBtn(2)}}
function showHot(){{renderTable(MD.slice(0,8));hlBtn(3)}}
function showVal(){{
  var v=[];MD.forEach(function(m){{
    if(Math.max(m.edge.home,m.edge.draw||0,m.edge.away)>2)v.push(m);
  }});
  renderTable(v);hlBtn(4);
}}
function hlBtn(i){{var bs=document.querySelectorAll('#tools .btn');bs.forEach(function(b,j){{b.className=j===i?'btn on':'btn'}})}}

renderTable(MD);renderVB();

function initCharts(){{
  if(typeof echarts==='undefined'){{setTimeout(initCharts,300);return}}
  var fb=MD.filter(function(m){{return m.sport==='football'}}).slice(0,10);
  var bb=MD.filter(function(m){{return m.sport==='basketball'}}).slice(0,5);
  var data=fb.concat(bb).slice(0,10);
  if(!data.length){{
    document.querySelectorAll('.cc').forEach(function(el){{el.innerHTML='<div style="text-align:center;padding:60px;color:#999">暂无数据</div>'}});
    return;
  }}
  var labels=data.map(function(m){{return m.home.substring(0,5)+' v '+m.away.substring(0,5)}});
  var tt={{trigger:'axis',backgroundColor:'#fff',borderColor:'#ccc',textStyle:{{color:'#333',fontSize:12}}}};
  var leg={{bottom:0,left:'center',textStyle:{{fontSize:mob?10:12}}}};
  var grd={{top:mob?'14%':'10%',bottom:'18%',left:'8%',right:'5%',containLabel:true}};
  
  // C1: 后验 vs 市场
  var c1=echarts.init(document.getElementById('c1'));
  c1.setOption({{tooltip:tt,legend:leg,grid:grd,
    xAxis:{{type:'category',data:labels,axisLabel:{{color:'#666',fontSize:mob?9:11,rotate:mob?35:20}}}},
    yAxis:{{type:'value',max:1,axisLabel:{{color:'#666',fontSize:10,formatter:function(v){{return(v*100)+'%'}}}},splitLine:{{lineStyle:{{color:'#e0e0e0'}}}}}},
    series:[
      {{name:'市场-主胜',type:'bar',itemStyle:{{color:'rgba(26,109,181,.3)'}},barGap:'5%',data:data.map(function(m){{return m.market.home}})}},
      {{name:'后验-主胜',type:'bar',itemStyle:{{color:'#1a6db5'}},data:data.map(function(m){{return m.bayes.home}})}},
      {{name:'市场-客胜',type:'bar',itemStyle:{{color:'rgba(198,40,40,.3)'}},barGap:'5%',data:data.map(function(m){{return m.market.away}})}},
      {{name:'后验-客胜',type:'bar',itemStyle:{{color:'#c62828'}},data:data.map(function(m){{return m.bayes.away}})}}
    ]}});
  
  // C2: 赔率离散度
  var hc=data.filter(function(m){{return m.companies&&Object.keys(m.companies).length>1}});
  var cl2=hc.slice(0,8).map(function(m){{return m.home.substring(0,5)}});
  var cn=[];hc.forEach(function(m){{Object.keys(m.companies).forEach(function(n){{if(cn.indexOf(n)<0)cn.push(n)}})}});
  var cs=['#1a6db5','#2e7d32','#e65100','#c62828','#00695c','#7b1fa2'];
  var s2=cn.slice(0,6).map(function(n,i){{return{{name:n.substring(0,8),type:'bar',itemStyle:{{color:cs[i%6]}},barGap:'8%',data:hc.slice(0,8).map(function(m){{return m.companies[n]?m.companies[n].home:null}})}}}});
  var c2=echarts.init(document.getElementById('c2'));
  c2.setOption({{tooltip:tt,legend:leg,grid:grd,
    xAxis:{{type:'category',data:cl2,axisLabel:{{color:'#666',fontSize:mob?9:11}}}},
    yAxis:{{type:'value',axisLabel:{{color:'#666',fontSize:10}},splitLine:{{lineStyle:{{color:'#e0e0e0'}}}}}},
    series:s2}});
  
  // C3: Edge排序
  var sv=VB.slice(0,12).reverse();
  var c3=echarts.init(document.getElementById('c3'));
  if(sv.length){{
    c3.setOption({{tooltip:{{trigger:'axis',backgroundColor:'#fff',borderColor:'#ccc',textStyle:{{color:'#333',fontSize:12}},formatter:function(p){{var d=sv[p[0].dataIndex];return'<b>'+d.n+'</b><br/>Edge: <b style="color:#2e7d32">+'+d.e.toFixed(1)+'%</b><br/>贝叶斯: '+(d.p*100).toFixed(1)+'%<br/>市场: '+(d.i*100).toFixed(1)+'%'}}}},
      grid:{{top:'5%',bottom:'8%',left:mob?'32%':'25%',right:'10%'}},
      xAxis:{{type:'value',axisLabel:{{color:'#666',fontSize:10,formatter:function(v){{return'+'+v+'%'}}}},splitLine:{{lineStyle:{{color:'#e0e0e0'}}}}}},
      yAxis:{{type:'category',data:sv.map(function(d){{return d.n}}),axisLabel:{{color:'#333',fontSize:mob?9:11,width:mob?90:140,overflow:'truncate'}}}},
      series:[{{type:'bar',data:sv.map(function(d){{return d.e}}),itemStyle:{{color:function(p){{return p.value>5?'#2e7d32':p.value>3?'#1a6db5':'#f5a623'}}}},barWidth:'60%',label:{{show:true,position:'right',fontSize:mob?9:11,color:'#333',fontFamily:'Tahoma',formatter:function(p){{return'+'+p.value.toFixed(1)+'%'}}}}}}]}});
  }}else{{
    document.getElementById('c3').innerHTML='<div style="text-align:center;padding:60px;color:#999">当前无 Edge&gt;2% 的机会</div>';
  }}
  
  // C4: 凯利 f*
  var c4=echarts.init(document.getElementById('c4'));
  c4.setOption({{tooltip:tt,legend:leg,grid:grd,
    xAxis:{{type:'category',data:labels,axisLabel:{{color:'#666',fontSize:mob?9:11}}}},
    yAxis:{{type:'value',min:0,max:0.15,axisLabel:{{color:'#666',fontSize:10,formatter:function(v){{return(v*100).toFixed(0)+'%'}}}},splitLine:{{lineStyle:{{color:'#e0e0e0'}}}}}},
    series:[
      {{name:'主胜 f*',type:'bar',itemStyle:{{color:'#2e7d32'}},barGap:'10%',data:data.map(function(m){{return m.kelly.home}})}},
      {{name:'平局 f*',type:'bar',itemStyle:{{color:'#f5a623'}},data:data.map(function(m){{return m.kelly.draw}})}},
      {{name:'客胜 f*',type:'bar',itemStyle:{{color:'#c62828'}},data:data.map(function(m){{return m.kelly.away}})}}
    ]}});
  
  // C5: 泊松热力图
  var fm=MD.filter(function(m){{return m.sport==='football'}})[0];
  var mx=fm?fm.poisson.matrix:[];
  var hg=['0球','1球','2球','3球','4球','5球'],ag=['0球','1球','2球','3球','4球','5球'];
  var hd=mx.map(function(s){{return[s.home,s.away,parseFloat((s.prob*100).toFixed(2))]}});
  var c5=echarts.init(document.getElementById('c5'));
  if(hd.length){{
    c5.setOption({{tooltip:{{backgroundColor:'#fff',borderColor:'#ccc',textStyle:{{color:'#333',fontSize:12}},formatter:function(p){{var v=p.value;return'比分 '+v[0]+'-'+v[1]+'<br/>概率: <b>'+v[2].toFixed(2)+'%</b>'}}}},
      grid:{{top:'8%',bottom:'14%',left:'12%',right:'10%'}},
      xAxis:{{type:'category',data:hg,name:(fm?fm.home:'主队')+'进球',nameTextStyle:{{color:'#666',fontSize:10}},axisLabel:{{color:'#666',fontSize:mob?9:11}}}},
      yAxis:{{type:'category',data:ag,name:(fm?fm.away:'客队')+'进球',nameTextStyle:{{color:'#666',fontSize:10}},axisLabel:{{color:'#666',fontSize:mob?9:11}}}},
      visualMap:{{min:0,max:15,orient:'horizontal',left:'center',bottom:0,inRange:{{color:['#f5f5f5','#c8e6c9','#66bb6a','#2e7d32','#1b5e20']}},textStyle:{{color:'#666',fontSize:10}}}},
      series:[{{type:'heatmap',data:hd,label:{{show:true,fontSize:mob?8:10,color:'#333',formatter:function(p){{return p.value[2]>=1?p.value[2].toFixed(1):''}}}},emphasis:{{itemStyle:{{shadowBlur:8,shadowColor:'rgba(0,0,0,.2)'}}}}}}]}});
  }}else{{
    document.getElementById('c5').innerHTML='<div style="text-align:center;padding:60px;color:#999">暂无足球赛事</div>';
  }}
  
  // C6: 凯利比值风控
  var c6=echarts.init(document.getElementById('c6'));
  c6.setOption({{tooltip:tt,legend:leg,grid:grd,
    xAxis:{{type:'category',data:labels,axisLabel:{{color:'#666',fontSize:mob?9:11}}}},
    yAxis:{{type:'value',min:0.7,max:1.15,axisLabel:{{color:'#666',fontSize:10}},splitLine:{{lineStyle:{{color:'#e0e0e0'}}}}}},
    series:[
      {{name:'主胜比值',type:'bar',itemStyle:{{color:'#2e7d32'}},barGap:'10%',data:data.map(function(m){{return m.kelly_ratio.home}})}},
      {{name:'平局比值',type:'bar',itemStyle:{{color:'#f5a623'}},data:data.map(function(m){{return m.kelly_ratio.draw}})}},
      {{name:'客胜比值',type:'bar',itemStyle:{{color:'#c62828'}},data:data.map(function(m){{return m.kelly_ratio.away}})}},
      {{name:'安全线(1.0)',type:'line',data:labels.map(function(){{return 1}}),lineStyle:{{color:'#999',type:'dashed'}},symbol:'none'}},
      {{name:'警戒线(1.05)',type:'line',data:labels.map(function(){{return 1.05}}),lineStyle:{{color:'#c62828',type:'dashed'}},symbol:'none'}}
    ]}});
  
  window.addEventListener('resize',function(){{c1.resize();c2.resize();c3.resize();c4.resize();c5.resize();c6.resize()}});
}}
initCharts();
</script>
</body>
</html>'''

def main():
    if not API_KEY:
        print('ERROR: ODDS_API_KEY not set')
        sys.exit(1)

    now = datetime.now(timezone.utc)
    update_time = now.strftime('%Y-%m-%d %H:%M UTC')
    print(f'=== 赛事赔率贝叶斯分析看板更新 v3 ===')
    print(f'时间: {update_time}')

    print('\n--- 足球赛事 ---')
    football_events = []
    for sport in FOOTBALL_SPORTS:
        events = fetch_odds(sport)
        football_events.extend(events)
        if len(football_events) >= 15:
            break

    print('\n--- 篮球赛事 ---')
    basketball_events = []
    for sport in BASKETBALL_SPORTS:
        events = fetch_odds(sport)
        basketball_events.extend(events)
        if len(basketball_events) >= 10:
            break

    print('\n--- 贝叶斯分析 ---')
    football_data = bayesian_analysis(football_events, 'football')
    basketball_data = bayesian_analysis(basketball_events, 'basketball')
    print(f'足球: {len(football_data)} 场分析完成')
    print(f'篮球: {len(basketball_data)} 场分析完成')

    if not football_data and not basketball_data:
        print('WARNING: No data to generate dashboard')
        sys.exit(0)

    # 验证 Edge 和 Kelly
    value_count = 0
    for m in football_data + basketball_data:
        if max(m['edge']['home'], m['edge']['draw'], m['edge']['away']) > 2:
            value_count += 1
    print(f'价值投注 (Edge>2%): {value_count} 个')

    # 打印前3场数据验证
    for m in (football_data + basketball_data)[:3]:
        print(f"  {m['match']}: edge={m['edge']}, kelly={m['kelly']}, kelly_ratio={m['kelly_ratio']}")

    print('\n--- 生成看板 ---')
    all_data = {
        'update_time': update_time,
        'football': football_data,
        'basketball': basketball_data
    }
    html = generate_html(all_data)
    output_path = os.path.join(REPO_DIR, 'index.html')
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'看板已写入: {output_path} ({len(html)//1024}KB)')

    # 写入 .nojekyll (防止 Jekyll 处理)
    nojekyll = os.path.join(REPO_DIR, '.nojekyll')
    with open(nojekyll, 'w') as f:
        f.write('')

    data_path = os.path.join(REPO_DIR, 'data.json')
    with open(data_path, 'w', encoding='utf-8') as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)
    print(f'数据已保存: {data_path}')

if __name__ == '__main__':
    main()
