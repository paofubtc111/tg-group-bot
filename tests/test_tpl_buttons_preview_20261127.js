// 欢迎模板按钮预览（tplButtonsPreviewHtml）提取测试。
// 跑法：node tests/test_tpl_buttons_preview_20261127.js
// 从 app/web/templates/index.html 里按花括号配对原样抽出 escapeHtml 与
// tplButtonsPreviewHtml，在 node 里跑，覆盖：
// - 空/无按钮 -> ''
// - 一行一个按钮 -> 一排一个按钮
// - 一行两个按钮 -> 同一排里两个按钮并排（一个 flex 行，两个按钮块）
// - 混合矩阵 [[A,B],[C]] -> 两排，共 3 个按钮
// - 颜色 class：danger 红 / success 绿 / primary 蓝 / 默认紫灰
// - 按钮文字转义（<script> 不原样输出）
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(
  path.join(__dirname, '..', 'app', 'web', 'templates', 'index.html'), 'utf8');

function extractFn(src, name) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('找不到函数 ' + name);
  let i = src.indexOf('{', start);
  let depth = 0;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error('函数 ' + name + ' 花括号不配对');
}

const code = extractFn(html, 'escapeHtml') + '\n' + extractFn(html, 'tplButtonsPreviewHtml');
eval(code); // eslint-disable-line no-eval

let ok = true;
function check(name, cond) {
  console.log((cond ? 'PASS ' : 'FAIL ') + name);
  if (!cond) ok = false;
}
function count(str, sub) { return str.split(sub).length - 1; }

check('空矩阵 -> 空字符串', tplButtonsPreviewHtml([]) === '');
check('null -> 空字符串', tplButtonsPreviewHtml(null) === '');
check('一行一个按钮 -> 一排一个按钮',
  (() => {
    const h = tplButtonsPreviewHtml([[{ text: '官网', url: 'https://example.com' }]]);
    return count(h, 'flex gap-1.5') === 1 && count(h, 'flex-1 ') === 1 && h.includes('官网');
  })());
check('一行两个按钮 -> 同一排两个按钮并排',
  (() => {
    const h = tplButtonsPreviewHtml([[{ text: '官网', url: 'https://example.com' },
      { text: '群规', url: 'https://t.me/x' }]]);
    return count(h, 'flex gap-1.5') === 1 && count(h, 'flex-1 ') === 2
      && h.includes('官网') && h.includes('群规');
  })());
check('混合矩阵 [[A,B],[C]] -> 两排共 3 个按钮',
  (() => {
    const h = tplButtonsPreviewHtml([[{ text: 'A', url: 'https://a' }, { text: 'B', url: 'https://b' }],
      [{ text: 'C', url: 'https://c' }]]);
    return count(h, 'flex gap-1.5') === 2 && count(h, 'flex-1 ') === 3;
  })());
check('danger 是红色', tplButtonsPreviewHtml([[{ text: '红', url: 'https://x', style: 'danger' }]]).includes('bg-red-500/25'));
check('success 是绿色', tplButtonsPreviewHtml([[{ text: '绿', url: 'https://x', style: 'success' }]]).includes('bg-emerald-500/25'));
check('primary 是蓝色', tplButtonsPreviewHtml([[{ text: '蓝', url: 'https://x', style: 'primary' }]]).includes('bg-sky-500/25'));
check('默认是紫灰色', tplButtonsPreviewHtml([[{ text: '默', url: 'https://x' }]]).includes('text-violet-300'));
check('按钮文字转义，不原样输出 <script>',
  (() => {
    const h = tplButtonsPreviewHtml([[{ text: '<script>alert(1)</script>', url: 'https://x' }]]);
    return !h.includes('<script>') && h.includes('&lt;script&gt;');
  })());

console.log(ok ? 'ALL PASS' : 'SOME FAILED');
process.exit(ok ? 0 : 1);
