#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站视频数据自动更新脚本
用法：python make/update_videos.py
功能：拉取 B站空间全部视频（公开 API，无需登录）→ 与 _data/videos.yml 现有
     手工字段（pdf/skill/featured/cover）按 BV 号合码 → 重写 episodes 列表。
规则：
  - ep 按发布时间从旧到新递增；episodes 列表按最新在前排列
  - title/desc/date/duration/url 以 B站 API 为准（真实数据，不编造）
  - pdf/skill/featured/cover 为手工字段，按 bvid 保留；新增视频默认无这些字段
  - featured 全站仅一期：保留原标记期；若原无则默认最新一期
依赖：仅标准库（urllib/hmac/json），无需安装任何包
"""
import re, json, time, hmac, hashlib, urllib.request, urllib.parse, http.cookiejar, os, sys

MID = '3494364577794314'          # B站空间 UID
YML = os.path.join(os.path.dirname(__file__), '..', '_data', 'videos.yml')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'
WBI_TAB = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
op.addheaders = [('User-Agent', UA), ('Referer', 'https://space.bilibili.com/'), ('Accept', 'application/json, text/plain, */*')]

def get(url, retries=3):
    for i in range(retries):
        try:
            return json.loads(op.open(url, timeout=20).read())
        except urllib.error.HTTPError as e:
            if e.code == 412 and i < retries - 1:
                time.sleep(5 * (i + 1)); continue
            raise

def addck(name, val):
    cj.set_cookie(http.cookiejar.Cookie(0, name, val, None, False, '.bilibili.com', True, True, '/', '/', False, False, None, False, None, None, {}))

def fetch_videos():
    # 1) 设备指纹 buvid3/buvid4
    spi = get('https://api.bilibili.com/x/frontend/finger/spi')
    addck('buvid3', spi['data']['b_3']); addck('buvid4', spi['data']['b_4'])
    # 2) bili_ticket（降低风控概率）
    ts = int(time.time())
    hexsign = hmac.new(b'XgwSnGZ1p', f'ts{ts}'.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request('https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket?key_id=ec02&hexsign=%s&context%%5Bts%%5D=%d&csrf=' % (hexsign, ts), method='POST', data=b'')
    tk = json.loads(op.open(req, timeout=20).read())
    if tk.get('code') != 0: raise SystemExit('bili_ticket 失败: %s' % tk.get('message'))
    addck('bili_ticket', tk['data']['ticket'])
    # 3) wbi 签名密钥（ticket 响应自带）
    img = tk['data']['nav']['img'].split('/')[-1].split('.')[0]
    sub = tk['data']['nav']['sub'].split('/')[-1].split('.')[0]
    raw = img + sub
    mixin = ''.join(raw[i] for i in WBI_TAB)[:32]
    # 4) 分页拉取
    out, pn = [], 1
    while True:
        params = {'mid': MID, 'ps': '30', 'pn': str(pn), 'order': 'pubdate', 'wts': int(time.time())}
        qs = urllib.parse.urlencode(sorted(params.items()))
        params['w_rid'] = hashlib.md5((qs + mixin).encode()).hexdigest()
        qs = urllib.parse.urlencode(sorted(params.items()))
        res = get('https://api.bilibili.com/x/space/wbi/arc/search?' + qs)
        if res.get('code') != 0: raise SystemExit('arc/search 失败: %s %s' % (res.get('code'), res.get('message')))
        vlist = ((res.get('data') or {}).get('list') or {}).get('vlist') or []
        out += vlist
        total = ((res.get('data') or {}).get('page') or {}).get('count') or 0
        if len(out) >= total or not vlist: break
        pn += 1; time.sleep(1)
    return out

def parse_manual(text):
    """从现有 yml 提取手工字段：bvid -> {pdf, skill, featured, cover}"""
    manual = {}
    for block in re.split(r'\n  - ', text.split('episodes:')[-1]):
        if 'url:' not in block: continue
        m = re.search(r'url:\s*.*?(BV[0-9A-Za-z]+)', block)
        if not m: continue
        bvid = m.group(1)
        entry = {}
        f = re.search(r'featured:\s*true', block)
        if f: entry['featured'] = True
        p = re.search(r'^\s*pdf:\s*\[(.+)\]', block, re.M)
        if p: entry['pdf'] = [x.strip().strip('\'"') for x in p.group(1).split(',')]
        s = re.search(r'^\s*skill:\s*(.+)', block, re.M)
        if s: entry['skill'] = s.group(1).strip().strip('\'"')
        c = re.search(r'^\s*cover:\s*(.+)', block, re.M)
        if c: entry['cover'] = c.group(1).strip().strip('\'"')
        manual[bvid] = entry
    return manual

def yq(s):
    """yaml 安全引号"""
    s = str(s).replace('"', '\\"')
    return '"%s"' % s

def main():
    # 离线模式：python make/update_videos.py <json路径>（跳过在线抓取，直接用已抓取数据）
    if len(sys.argv) > 1:
        src = json.load(open(sys.argv[1], encoding='utf-8'))
        print('离线模式，视频数:', len(src))
    else:
        src = fetch_videos()
        print('API 返回视频数:', len(src))
    old = open(YML, encoding='utf-8').read()
    header = old.split('episodes:')[0] + 'episodes:\n'
    manual = parse_manual(old)

    # 按发布时间升序分配 ep
    src.sort(key=lambda v: v.get('created') or 0)
    lines = []
    featured_bvid = next((b for b, e in manual.items() if e.get('featured')), None)
    if not featured_bvid and src:
        featured_bvid = src[-1]['bvid']  # 无标记期 → 默认最新一期
    for idx, v in enumerate(src, 1):
        bvid = v['bvid']
        man = manual.get(bvid, {})
        date = time.strftime('%Y-%m-%d', time.localtime(v['created'])) if v.get('created') else ''
        desc = (v.get('desc') or '').replace('\n', ' ').strip()
        if len(desc) > 90: desc = desc[:90].rstrip('，。；; ') + '……'
        if desc in ('-', ''): desc = v.get('title', '')
        buf = ['  - ep: %d' % idx,
               '    title: %s' % yq(v.get('title', '')),
               '    desc: %s' % yq(desc),
               '    date: %s' % date,
               '    duration: %s' % yq(v.get('length', '')),
               '    cover: %s' % yq(man.get('cover', '')),
               '    url: https://www.bilibili.com/video/%s' % bvid,
               '    featured: %s' % ('true' if bvid == featured_bvid else 'false')]
        if man.get('pdf'): buf.append('    pdf: [%s]' % ', '.join(man['pdf']))
        if man.get('skill'): buf.append('    skill: %s' % yq(man['skill']))
        lines.append('\n'.join(buf))

    new = header + '\n'.join(lines) + '\n'
    open(YML, 'w', encoding='utf-8', newline='\n').write(new)
    print('已写入 %s（%d 期，featured=%s）' % (os.path.normpath(YML), len(src), featured_bvid))
    print('提示：git diff _data/videos.yml 核对后提交推送。')

if __name__ == '__main__':
    main()
