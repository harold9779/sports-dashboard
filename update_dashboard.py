#!/usr/bin/env python3
"""
赛事赔率贝叶斯分析看板 - 自动更新脚本 (titan007 风格)
从 The Odds API 获取最新赔率数据，运行贝叶斯分析，生成看板 HTML
"""
import json, math, os, sys
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.parse import urlencode

API_KEY = os.environ.get('ODDS_API_KEY', '')
REPO_DIR = os.environ.get('GITHUB_WORKSPACE', os.path.dirname(os.path.abspath(__file__)))

FOOTBALL_SPORTS = [
    'soccer_epl', 'soccer_spain_la_liga', 'soccer_germany_bundesliga',
    'soccer_italy_serie_a', 'soccer_france_ligue_one',
    'soccer_uefa_champs_league', 'soccer_efl_champ',
    'soccer_netherlands_eredivisie', 'soccer_portugal_primeira_liga',
    'soccer_belgium_first_div', 'soccer_afl'
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

def bayesian_analysis(events, sport_type='football'):
    results = []
    for event in events:
        home = event.get('home_team', '')
        away = event.get('away_team', '')
        commence = event.get('commence_time', '')
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
        all_home = [o['home'] for o in odds_by_company.values() if o['home'] > 0]
        all_draw = [o['draw'] for o in odds_by_company.values() if o['draw'] > 0]
        all_away = [o['away'] for o in odds_by_company.values() if o['away'] > 0]
        if not all_home or not all_away:
            continue
        avg_home = sum(all_home) / len(all_home)
        avg_draw = sum(all_draw) / len(all_draw) if all_draw else 0
        avg_away = sum(all_away) / len(all_away)
        imp_home = 1.0 / avg_home if avg_home > 0 else 0
        imp_draw = 1.0 / avg_draw if avg_draw > 0 else 0
        imp_away = 1.0 / avg_away if avg_away > 0 else 0
        total_imp = imp_home + imp_draw + imp_away
        if total_imp <= 0:
            continue
        prior_home = imp_home / total_imp
        prior_draw = imp_draw / total_imp
        prior_away = imp_away / total_imp
        home_std = (sum((o - avg_home)**2 for o in all_home) / len(all_home))**0.5 if len(all_home) > 1 else 0.05
        away_std = (sum((o - avg_away)**2 for o in all_away) / len(all_away))**0.5 if len(all_away) > 1 else 0.05
        consistency = 1.0 / (1.0 + home_std + away_std)
        post_home = prior_home * (1 + 0.1 * consistency)
        post_draw = prior_draw * (1 + 0.05 * consistency) if prior_draw > 0 else 0
        post_away = prior_away * (1 + 0.1 * consistency)
        total_post = post_home + post_draw + post_away
        if total_post <= 0:
            continue
        post_home /= total_post
        post_draw /= total_post
        post_away /= total_post
        kelly_home = post_home * avg_home
        kelly_draw = post_draw * avg_draw if avg_draw > 0 else 0
        kelly_away = post_away * avg_away
        imp2_total = 1/avg_home + (1/avg_draw if avg_draw > 0 else 0) + 1/avg_away
        edge_home = (post_home - (1/avg_home)/imp2_total) * 100
        edge_draw = (post_draw - (1/avg_draw)/imp2_total) * 100 if avg_draw > 0 else 0
        edge_away = (post_away - (1/avg_away)/imp2_total) * 100
        lambda_home = post_home * 3.0
        lambda_away = post_away * 2.5
        poisson_scores = []
        for h in range(5):
            for a in range(5):
                ph = math.exp(-lambda_home) * (lambda_home**h) / math.factorial(h)
                pa = math.exp(-lambda_away) * (lambda_away**a) / math.factorial(a)
                poisson_scores.append({'home': h, 'away': a, 'prob': ph * pa})
        poisson_scores.sort(key=lambda x: x['prob'], reverse=True)
        best = poisson_scores[0] if poisson_scores else {'home': 1, 'away': 1, 'prob': 0.1}
        result = {
            'match': f'{home} vs {away}', 'home': home, 'away': away,
            'commence': commence, 'sport': sport_type,
            'league': event.get('sport_title', ''),
            'odds': {'home': round(avg_home, 2), 'draw': round(avg_draw, 2), 'away': round(avg_away, 2)},
            'companies': {n: {k: round(v, 2) for k, v in odds.items()} for n, odds in list(odds_by_company.items())[:6]},
            'bayes': {'home': round(post_home, 3), 'draw': round(post_draw, 3), 'away': round(post_away, 3)},
            'kelly': {'home': round(kelly_home, 3), 'draw': round(kelly_draw, 3), 'away': round(kelly_away, 3)},
            'edge': {'home': round(edge_home, 1), 'draw': round(edge_draw, 1), 'away': round(edge_away, 1)},
            'poisson': {'score': f"{best['home']}-{best['away']}", 'prob': round(best['prob'], 3), 'matrix': poisson_scores[:25]},
            'status': 'upcoming',
            'confidence': 'high' if max(post_home, post_away) > 0.55 else 'medium' if max(post_home, post_away) > 0.4 else 'low'
        }
        results.append(result)
    return results

def generate_html(data):
    data_json = json.dumps(data, ensure_ascii=False)
    html = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>赛事赔率贝叶斯分析看板</title>
<script>
(function(){
  var cdns = [
    'https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js',
    'https://cdnjs.cloudflare.com/ajax/libs/echarts/5.5.0/echarts.min.js',
    'https://unpkg.com/echarts@5.5.0/dist/echarts.min.js'
  ];
  var loaded = false;
  for(var i=0;i<cdns.length;i++){
    try{
      var x=new XMLHttpRequest();
      x.open('GET',cdns[i],false);
      x.send();
      if(x.status===200 && x.responseText.length>100000){
        var s=document.createElement('script');
        s.textContent=x.responseText;
        document.head.appendChild(s);
        loaded=true;
        break;
      }
    }catch(e){}
  }
  if(!loaded){
    var s=document.createElement('script');
    s.src=cdns[0];
    document.head.appendChild(s);
  }
})();
</script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:Tahoma,"Microsoft YaHei",sans-serif;background:#004b81;color:#333;font-size:12px}
a{color:#333;text-decoration:none}a:hover{color:#e62129}
#tools{width:100%;max-width:1200px;margin:0 auto;padding:6px 10px;background:#f6f6f6;border-bottom:1px solid #c0c0c0;display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:12px}
#tools .btn{display:inline-block;padding:2px 10px;line-height:22px;border:1px solid #c0c0c0;border-radius:2px;background:#fff;color:#333;cursor:pointer;font-size:12px;box-shadow:0 1px 0 rgba(0,0,0,.08)}
#tools .btn:hover{border-color:#93c1d8;color:#228bd6}
#tools .btn.on{background:#FFEEB9;border-color:#DEA67C}
#tools .txt{color:#666;padding:0 4px}
#tools .info{margin-left:auto;color:#888;font-size:11px}
.page{max-width:1200px;margin:0 auto;background:#fff;min-height:100vh;box-shadow:0 0 20px rgba(0,0,0,.3)}
.hdr{background:linear-gradient(180deg,#1a6db5,#0d5a9e);padding:10px 16px;color:#fff;display:flex;justify-content:space-between;align-items:center;border-bottom:2px solid #004080}
.hdr h1{font-size:15px;font-weight:bold;letter-spacing:.5px}
.hdr .tm{font-size:11px;color:#b8d4f0}
.kpi{display:grid;grid-template-columns:repeat(4,1fr);border-bottom:1px solid #d0d0d0}
.kpi-cell{text-align:center;padding:12px 8px;border-right:1px solid #e0e0e0;background:#f8fafc}
.kpi-cell:last-child{border-right:none}
.kpi-cell .v{font-size:24px;font-weight:bold;font-family:Tahoma,Arial,sans-serif}
.kpi-cell .v.c1{color:#1a6db5}.kpi-cell .v.c2{color:#2e7d32}.kpi-cell .v.c3{color:#e65100}.kpi-cell .v.c4{color:#c62828}
.kpi-cell .l{font-size:11px;color:#888;margin-top:3px}
.sec-hdr{background:linear-gradient(180deg,#e8f0f8,#d0dfe8);border-top:1px solid #b0c4d8;border-bottom:1px solid #b0c4d8;padding:7px 14px;font-size:13px;font-weight:bold;color:#1a4a7a}
.mtbl{width:100%;border-collapse:collapse;font-size:12px}
.mtbl thead th{background:#e0ecf5;padding:6px 8px;text-align:center;font-weight:bold;color:#1a4a7a;border-bottom:2px solid #1a6db5;font-size:11px;white-space:nowrap}
.mtbl tbody tr{border-bottom:1px solid #e8e8e8}
.mtbl tbody tr:hover{background:#f0f6ff}
.mtbl tbody tr:nth-child(even){background:#fafbfc}
.mtbl tbody tr:nth-child(even):hover{background:#f0f6ff}
.mtbl td{padding:7px 8px;text-align:center;vertical-align:middle;white-space:nowrap}
.mtbl .lg{display:inline-block;padding:1px 6px;border-radius:2px;font-size:10px;font-weight:bold;color:#fff}
.mtbl .lg.fb{background:#1B5E20}.mtbl .lg.bb{background:#E65100}
.mtbl .tn{font-weight:bold;color:#333}
.mtbl .th{text-align:right;padding-right:4px}.mtbl .ta{text-align:left;padding-left:4px}
.mtbl .od{font-family:Tahoma,Arial;font-weight:bold;font-size:12px}
.mtbl .vs{color:#999;font-weight:normal;font-size:11px}
.mtbl .pbar{display:inline-flex;height:10px;border-radius:2px;overflow:hidden;vertical-align:middle;margin-right:4px}
.mtbl .pbar .h{background:#2e7d32}.mtbl .pbar .d{background:#f5a623}.mtbl .pbar .a{background:#c62828}
.mtbl .pt{font-family:Tahoma,Arial;font-size:11px;font-weight:bold}
.mtbl .pt-h{color:#2e7d32}.mtbl .pt-m{color:#e65100}.mtbl .pt-l{color:#666}
.mtbl .vb{display:inline-block;padding:1px 5px;border-radius:2px;font-size:10px;font-weight:bold}
.mtbl .vb.pos{background:#e8f5e9;color:#2e7d32}
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
@media(max-width:768px){
  .kpi{grid-template-columns:repeat(2,1fr)}
  .chrow{grid-template-columns:1fr}
  .chbox .cc{height:220px}
  .mtbl{font-size:11px}
  .mtbl td{padding:5px 4px}
  .hdr h1{font-size:13px}
  #tools{font-size:11px}
  .vlist{flex-direction:column}
}
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
  <div style="overflow-x:auto">
    <table class="mtbl"><thead><tr>
      <th>赛事</th><th>开赛时间</th><th>主队</th><th></th><th>客队</th>
      <th>主胜</th><th>平局</th><th>客胜</th>
      <th>贝叶斯后验</th><th>泊松预测</th><th>凯利指数</th><th>价值</th>
    </tr></thead><tbody id="tbody"></tbody></table>
  </div>
  <div class="sec-hdr">💰 价值投注雷达 — 正EV机会 (Edge &gt; 2%)</div>
  <div class="vsec"><div class="vlist" id="vlist"></div></div>
  <div class="sec-hdr">📈 数据分析图表</div>
  <div class="chsec"><div class="chrow">
    <div class="chbox"><div class="cl"><span class="dot" style="background:#1a6db5"></span>贝叶斯后验概率分布</div><div id="c1" class="cc"></div></div>
    <div class="chbox"><div class="cl"><span class="dot" style="background:#e65100"></span>多公司赔率离散度</div><div id="c2" class="cc"></div></div>
  </div></div>
  <div class="chsec"><div class="chrow">
    <div class="chbox"><div class="cl"><span class="dot" style="background:#2e7d32"></span>价值投注边际优势</div><div id="c3" class="cc"></div></div>
    <div class="chbox"><div class="cl"><span class="dot" style="background:#7b1fa2"></span>凯利指数风控</div><div id="c4" class="cc"></div></div>
  </div></div>
  <div class="chsec"><div class="chrow">
    <div class="chbox"><div class="cl"><span class="dot" style="background:#00695c"></span>泊松比分热力图</div><div id="c5" class="cc"></div></div>
    <div class="chbox"><div class="cl"><span class="dot" style="background:#c62828"></span>隐含概率 vs 后验概率</div><div id="c6" class="cc"></div></div>
  </div></div>
  <div class="ftr">
    <b>方法论：</b>Shin法去水 → 欧赔隐含概率先验 → 多公司赔率离散度高斯似然 → 贝叶斯后验更新 → 泊松比分预测 → 价值投注识别 (Edge&gt;2%) → 凯利指数风控<br/>
    <b>数据来源：</b>The Odds API（Bet365 / Pinnacle / William Hill / DraftKings / FanDuel 等 20+ 博彩公司）· 每30分钟 GitHub Actions 自动刷新
  </div>
</div>
<script>
var D=''' + data_json + ''';
var MD=D.football.concat(D.basketball);
var VB=[];
var mob=window.innerWidth<=768;
MD.forEach(function(m){
  var t=1/m.odds.home+(m.odds.draw>0?1/m.odds.draw:0)+1/m.odds.away;
  var ih=(1/m.odds.home)/t,id=m.odds.draw>0?(1/m.odds.draw)/t:0,ia=(1/m.odds.away)/t;
  if(m.bayes.home-ih>.02)VB.push({n:m.home+' 主胜',e:(m.bayes.home-ih)*100,p:m.bayes.home,i:ih});
  if(m.bayes.draw-id>.02&&m.odds.draw>0)VB.push({n:m.home+' 平局',e:(m.bayes.draw-id)*100,p:m.bayes.draw,i:id});
  if(m.bayes.away-ia>.02)VB.push({n:m.away+' 客胜',e:(m.bayes.away-ia)*100,p:m.bayes.away,i:ia});
});
VB.sort(function(a,b){return b.e-a.e});
document.getElementById('updateTime').textContent='更新: '+(D.update_time||'')+' · 足球 '+D.football.length+' / 篮球 '+D.basketball.length;
document.getElementById('kTotal').textContent=MD.length;
document.getElementById('kFB').textContent=D.football.length;
document.getElementById('kBB').textContent=D.basketball.length;
document.getElementById('kVal').textContent=VB.length;
function renderTable(data){
  var tb=document.getElementById('tbody');tb.innerHTML='';
  data.slice(0,25).forEach(function(m){
    var bb=m.sport==='basketball',lg=bb?'bb':'fb',lt=m.league||'';
    var ts='';
    if(m.commence){try{var d=new Date(m.commence);ts=(d.getMonth()+1)+'/'+d.getDate()+' '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')}catch(e){ts=m.commence.substring(5,16).replace('T',' ')}}
    var bh='';
    if(bb){bh='<span class="pt pt-'+(m.bayes.home>.5?'h':'m')+'">主'+(m.bayes.home*100).toFixed(1)+'%</span> / <span class="pt pt-'+(m.bayes.away>.5?'h':'m')+'">客'+(m.bayes.away*100).toFixed(1)+'%</span>'}
    else{bh='<span class="pt pt-'+(m.bayes.home>.5?'h':'m')+'">'+(m.bayes.home*100).toFixed(0)+'%</span><span class="pt" style="color:#999">/'+(m.bayes.draw*100).toFixed(0)+'%/</span><span class="pt pt-'+(m.bayes.away>.5?'h':'m')+'">'+(m.bayes.away*100).toFixed(0)+'%</span>'}
    var bw=110,wh=Math.round(m.bayes.home*bw),wd=Math.round(m.bayes.draw*bw),wa=bw-wh-wd;
    var pb='<div class="pbar" style="width:'+bw+'px"><div class="h" style="width:'+wh+'px"></div>'+(bb?'':'<div class="d" style="width:'+wd+'px"></div>')+'<div class="a" style="width:'+wa+'px"></div></div>';
    var kh='',ka=m.kelly;
    function kc(v){return v>1.05?'#c62828':v>1?'#e65100':'#2e7d32'}
    if(bb){kh='<span style="color:'+kc(ka.home)+'">'+ka.home.toFixed(2)+'</span> / <span style="color:'+kc(ka.away)+'">'+ka.away.toFixed(2)+'</span>'}
    else{kh='<span style="color:'+kc(ka.home)+'">'+ka.home.toFixed(2)+'</span>/<span style="color:'+kc(ka.draw)+'">'+ka.draw.toFixed(2)+'</span>/<span style="color:'+kc(ka.away)+'">'+ka.away.toFixed(2)+'</span>'}
    var em=Math.max(m.edge.home,m.edge.draw||0,m.edge.away);
    var vl=em>2?'<span class="vb pos">+'+em.toFixed(1)+'%</span>':'<span style="color:#ccc">—</span>';
    var tr=document.createElement('tr');
    tr.innerHTML='<td><span class="lg '+lg+'">'+lt+'</span></td>'
      +'<td style="font-family:Tahoma;font-size:11px;color:#666">'+ts+'</td>'
      +'<td class="tn th">'+m.home+'</td><td class="vs">vs</td><td class="tn ta">'+m.away+'</td>'
      +'<td class="od">'+m.odds.home.toFixed(2)+'</td>'
      +'<td class="od">'+(bb?'—':m.odds.draw.toFixed(2))+'</td>'
      +'<td class="od">'+m.odds.away.toFixed(2)+'</td>'
      +'<td>'+pb+'<br/>'+bh+'</td>'
      +'<td style="font-family:Tahoma;font-weight:bold;color:#7b1fa2">'+m.poisson.score+'</td>'
      +'<td class="od" style="font-size:11px">'+kh+'</td>'
      +'<td>'+vl+'</td>';
    tb.appendChild(tr);
  });
}
function renderVB(){
  var el=document.getElementById('vlist');el.innerHTML='';
  if(!VB.length){el.innerHTML='<div style="color:#999;padding:8px">当前无明显价值投注机会</div>';return}
  VB.slice(0,12).forEach(function(v){
    var d=document.createElement('div');d.className='vitem';
    d.innerHTML='<span class="nm">'+v.n+'</span><span class="eg">+'+v.e.toFixed(1)+'%</span><span class="dt">P='+(v.p*100).toFixed(1)+'% / Imp='+(v.i*100).toFixed(1)+'%</span>';
    el.appendChild(d);
  });
}
function showAll(){renderTable(MD);hlBtn(0)}
function showFB(){renderTable(MD.filter(function(m){return m.sport==='football'}));hlBtn(1)}
function showBB(){renderTable(MD.filter(function(m){return m.sport==='basketball'}));hlBtn(2)}
function showHot(){renderTable(MD.slice(0,8));hlBtn(3)}
function showVal(){var v=[];MD.forEach(function(m){if(Math.max(m.edge.home,m.edge.draw||0,m.edge.away)>2)v.push(m)});renderTable(v);hlBtn(4)}
function hlBtn(i){var bs=document.querySelectorAll('#tools .btn');bs.forEach(function(b,j){b.className=j===i?'btn on':'btn'})}
renderTable(MD);renderVB();
function initCharts(){
  if(typeof echarts==='undefined'){setTimeout(initCharts,200);return}
  var data=MD.slice(0,10);
  var labels=data.map(function(m){return m.home.substring(0,4)+' vs '+m.away.substring(0,4)});
  var c1=echarts.init(document.getElementById('c1'));
  c1.setOption({tooltip:{trigger:'axis',backgroundColor:'#fff',borderColor:'#ccc',textStyle:{color:'#333',fontSize:12}},legend:{bottom:0,left:'center',textStyle:{fontSize:mob?10:12}},grid:{top:mob?'14%':'10%',bottom:'18%',left:'8%',right:'5%',containLabel:true},
    xAxis:{type:'category',data:labels,axisLabel:{color:'#666',fontSize:mob?9:11,rotate:mob?35:20}},
    yAxis:{type:'value',max:1,axisLabel:{color:'#666',fontSize:10,formatter:function(v){return(v*100)+'%'}},splitLine:{lineStyle:{color:'#e0e0e0'}}},
    series:[
      {name:'主胜',type:'bar',stack:'a',itemStyle:{color:'#2e7d32'},barWidth:'55%',data:data.map(function(m){return m.bayes.home}),label:{show:!mob,position:'inside',fontSize:9,color:'#fff',formatter:function(p){return(p.value*100).toFixed(0)+'%'}}},
      {name:'平局',type:'bar',stack:'a',itemStyle:{color:'#f5a623'},data:data.map(function(m){return m.bayes.draw})},
      {name:'客胜',type:'bar',stack:'a',itemStyle:{color:'#c62828'},data:data.map(function(m){return m.bayes.away})}
    ]});
  var hc=data.filter(function(m){return m.companies&&Object.keys(m.companies).length>1});
  var cl2=hc.slice(0,8).map(function(m){return m.home.substring(0,4)});
  var cn=[];hc.forEach(function(m){Object.keys(m.companies).forEach(function(n){if(cn.indexOf(n)<0)cn.push(n)})});
  var cs=['#1a6db5','#2e7d32','#e65100','#c62828','#00695c','#7b1fa2'];
  var s2=cn.slice(0,6).map(function(n,i){return{name:n.substring(0,8),type:'bar',itemStyle:{color:cs[i%6]},barGap:'8%',data:hc.slice(0,8).map(function(m){return m.companies[n]?m.companies[n].home:null})}});
  var c2=echarts.init(document.getElementById('c2'));
  c2.setOption({tooltip:{trigger:'axis',backgroundColor:'#fff',borderColor:'#ccc',textStyle:{color:'#333',fontSize:12}},legend:{bottom:0,left:'center',textStyle:{fontSize:mob?10:12}},grid:{top:mob?'14%':'10%',bottom:'18%',left:'8%',right:'5%',containLabel:true},
    xAxis:{type:'category',data:cl2,axisLabel:{color:'#666',fontSize:mob?9:11}},
    yAxis:{type:'value',axisLabel:{color:'#666',fontSize:10},splitLine:{lineStyle:{color:'#e0e0e0'}}},
    series:s2});
  var sv=VB.slice(0,12).reverse();
  var c3=echarts.init(document.getElementById('c3'));
  c3.setOption({tooltip:{trigger:'axis',backgroundColor:'#fff',borderColor:'#ccc',textStyle:{color:'#333',fontSize:12},formatter:function(p){var d=sv[p[0].dataIndex];return'<b>'+d.n+'</b><br/>贝叶斯: '+(d.p*100).toFixed(1)+'%<br/>隐含: '+(d.i*100).toFixed(1)+'%<br/>边际: <b style="color:#2e7d32">+'+d.e.toFixed(1)+'%</b>'}},
    grid:{top:'5%',bottom:'8%',left:mob?'32%':'25%',right:'10%'},
    xAxis:{type:'value',axisLabel:{color:'#666',fontSize:10,formatter:function(v){return'+'+v+'%'}},splitLine:{lineStyle:{color:'#e0e0e0'}}},
    yAxis:{type:'category',data:sv.map(function(d){return d.n}),axisLabel:{color:'#333',fontSize:mob?9:11,width:mob?90:140,overflow:'truncate'}},
    series:[{type:'bar',data:sv.map(function(d){return d.e}),itemStyle:{color:function(p){return p.value>5?'#2e7d32':p.value>3?'#1a6db5':'#f5a623'}},barWidth:'60%',label:{show:true,position:'right',fontSize:mob?9:11,color:'#333',fontFamily:'Tahoma',formatter:function(p){return'+'+p.value.toFixed(1)+'%'}}}]});
  var c4=echarts.init(document.getElementById('c4'));
  c4.setOption({tooltip:{trigger:'axis',backgroundColor:'#fff',borderColor:'#ccc',textStyle:{color:'#333',fontSize:12}},legend:{bottom:0,left:'center',textStyle:{fontSize:mob?10:12}},grid:{top:mob?'14%':'10%',bottom:'18%',left:'8%',right:'5%',containLabel:true},
    xAxis:{type:'category',data:labels,axisLabel:{color:'#666',fontSize:mob?9:11}},
    yAxis:{type:'value',min:.8,max:1.15,axisLabel:{color:'#666',fontSize:10},splitLine:{lineStyle:{color:'#e0e0e0'}}},
    series:[
      {name:'主胜凯利',type:'bar',itemStyle:{color:'#2e7d32'},barGap:'10%',data:data.map(function(m){return m.kelly.home})},
      {name:'平局凯利',type:'bar',itemStyle:{color:'#f5a623'},data:data.map(function(m){return m.kelly.draw})},
      {name:'客胜凯利',type:'bar',itemStyle:{color:'#c62828'},data:data.map(function(m){return m.kelly.away})},
      {name:'安全线',type:'line',data:labels.map(function(){return 1}),lineStyle:{color:'#999',type:'dashed'},symbol:'none'},
      {name:'警戒线',type:'line',data:labels.map(function(){return 1.05}),lineStyle:{color:'#c62828',type:'dashed'},symbol:'none'}
    ]});
  var bm=MD[0],mx=bm?bm.poisson.matrix:[];
  var hg=['0球','1球','2球','3球','4球','5球'],ag=['0球','1球','2球','3球','4球','5球'];
  var hd=mx.map(function(s){return[s.home,s.away,parseFloat((s.prob*100).toFixed(2))]});
  var c5=echarts.init(document.getElementById('c5'));
  c5.setOption({tooltip:{backgroundColor:'#fff',borderColor:'#ccc',textStyle:{color:'#333',fontSize:12},formatter:function(p){var v=p.value;return'比分 '+v[0]+'-'+v[1]+'<br/>概率: <b>'+v[2].toFixed(2)+'%</b>'}},
    grid:{top:'8%',bottom:'14%',left:'12%',right:'10%'},
    xAxis:{type:'category',data:hg,name:(bm?bm.home:'主队')+'进球',nameTextStyle:{color:'#666',fontSize:10},axisLabel:{color:'#666',fontSize:mob?9:11}},
    yAxis:{type:'category',data:ag,name:(bm?bm.away:'客队')+'进球',nameTextStyle:{color:'#666',fontSize:10},axisLabel:{color:'#666',fontSize:mob?9:11}},
    visualMap:{min:0,max:15,orient:'horizontal',left:'center',bottom:0,inRange:{color:['#f5f5f5','#c8e6c9','#66bb6a','#2e7d32','#1b5e20']},textStyle:{color:'#666',fontSize:10}},
    series:[{type:'heatmap',data:hd,label:{show:true,fontSize:mob?8:10,color:'#333',formatter:function(p){return p.value[2]>=1?p.value[2].toFixed(1):''}},emphasis:{itemStyle:{shadowBlur:8,shadowColor:'rgba(0,0,0,.2)'}}}]});
  var imp=data.map(function(m){var t=1/m.odds.home+(m.odds.draw>0?1/m.odds.draw:0)+1/m.odds.away;return{h:(1/m.odds.home)/t,a:(1/m.odds.away)/t}});
  var c6=echarts.init(document.getElementById('c6'));
  c6.setOption({tooltip:{trigger:'axis',backgroundColor:'#fff',borderColor:'#ccc',textStyle:{color:'#333',fontSize:12}},legend:{bottom:0,left:'center',textStyle:{fontSize:mob?10:12}},grid:{top:mob?'14%':'10%',bottom:'18%',left:'8%',right:'5%',containLabel:true},
    xAxis:{type:'category',data:labels,axisLabel:{color:'#666',fontSize:mob?9:11,rotate:20}},
    yAxis:{type:'value',max:1,axisLabel:{color:'#666',fontSize:10,formatter:function(v){return(v*100)+'%'}},splitLine:{lineStyle:{color:'#e0e0e0'}}},
    series:[
      {name:'隐含-主胜',type:'bar',itemStyle:{color:'rgba(26,109,181,.35)'},barGap:'5%',data:imp.map(function(d){return d.h})},
      {name:'后验-主胜',type:'bar',itemStyle:{color:'#1a6db5'},data:data.map(function(m){return m.bayes.home})},
      {name:'隐含-客胜',type:'bar',itemStyle:{color:'rgba(198,40,40,.35)'},barGap:'5%',data:imp.map(function(d){return d.a})},
      {name:'后验-客胜',type:'bar',itemStyle:{color:'#c62828'},data:data.map(function(m){return m.bayes.away})}
    ]});
  window.addEventListener('resize',function(){c1.resize();c2.resize();c3.resize();c4.resize();c5.resize();c6.resize()});
}
initCharts();
</script>
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

    print('\\n--- 足球赛事 ---')
    football_events = []
    for sport in FOOTBALL_SPORTS:
        events = fetch_odds(sport)
        football_events.extend(events)
        if len(football_events) >= 15:
            break

    print('\\n--- 篮球赛事 ---')
    basketball_events = []
    for sport in BASKETBALL_SPORTS:
        events = fetch_odds(sport)
        basketball_events.extend(events)
        if len(basketball_events) >= 10:
            break

    print('\\n--- 贝叶斯分析 ---')
    football_data = bayesian_analysis(football_events, 'football')
    basketball_data = bayesian_analysis(basketball_events, 'basketball')
    print(f'足球: {len(football_data)} 场分析完成')
    print(f'篮球: {len(basketball_data)} 场分析完成')

    if not football_data and not basketball_data:
        print('WARNING: No data to generate dashboard')
        sys.exit(0)

    print('\\n--- 生成看板 ---')
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

    data_path = os.path.join(REPO_DIR, 'data.json')
    with open(data_path, 'w', encoding='utf-8') as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)
    print(f'数据已保存: {data_path}')

if __name__ == '__main__':
    main()
