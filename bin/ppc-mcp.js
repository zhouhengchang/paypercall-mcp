#!/usr/bin/env node
/**
 * paypercall-mcp — stdio MCP 桥
 *
 * 作用：让只支持 stdio 的 MCP 客户端（Claude Desktop / Cursor / 各类 IDE 插件）
 * 连上远端的按次计费网关。所有 JSON-RPC 消息原样转发到 gatewayUrl/mcp，
 * 计费、鉴权、结算全部在服务端完成，客户端拿不到任何绕过计费的路径。
 *
 * 环境变量：
 *   PPC_GATEWAY   网关地址，默认 http://127.0.0.1:8300
 *   PPC_API_KEY   预充值钱包的 API Key（没有就依赖 x402 付款头）
 *   PPC_PAYMENT   x402 的 X-PAYMENT 载荷（一般由支持 x402 的客户端注入）
 *   PPC_DEBUG     设为 1 打印转发日志到 stderr
 */

'use strict';

const http = require('http');
const https = require('https');
const readline = require('readline');

const GATEWAY = (process.env.PPC_GATEWAY || 'http://127.0.0.1:8300').replace(/\/+$/, '');
const API_KEY = process.env.PPC_API_KEY || '';
const PAYMENT = process.env.PPC_PAYMENT || '';
const DEBUG = process.env.PPC_DEBUG === '1';

function log() {
  if (!DEBUG) return;
  process.stderr.write('[ppc-mcp] ' + Array.prototype.slice.call(arguments).join(' ') + '\n');
}

function post(body) {
  return new Promise(function (resolve, reject) {
    let url;
    try {
      url = new URL(GATEWAY + '/mcp');
    } catch (e) {
      return reject(new Error('PPC_GATEWAY 不是合法地址：' + GATEWAY));
    }
    const lib = url.protocol === 'https:' ? https : http;
    const payload = Buffer.from(JSON.stringify(body), 'utf8');
    const headers = {
      'Content-Type': 'application/json',
      'Content-Length': payload.length,
      'Accept': 'application/json',
    };
    if (API_KEY) headers['X-API-Key'] = API_KEY;
    if (PAYMENT) headers['X-PAYMENT'] = PAYMENT;

    const req = lib.request({
      protocol: url.protocol,
      hostname: url.hostname,
      port: url.port || (url.protocol === 'https:' ? 443 : 80),
      path: url.pathname,
      method: 'POST',
      headers: headers,
      timeout: 120000,
    }, function (res) {
      const chunks = [];
      res.on('data', function (c) { chunks.push(c); });
      res.on('end', function () {
        const text = Buffer.concat(chunks).toString('utf8');
        log('HTTP', res.statusCode, text.slice(0, 200));
        if (res.statusCode === 402) {
          let info = {};
          try { info = JSON.parse(text); } catch (e) { /* ignore */ }
          const pr = info && (info.payment_required ||
            (info.error && info.error.data));
          const price = pr && pr.price_usd != null ? pr.price_usd : '?';
          const reason = info && info.x402Error ? '（' + info.x402Error + '）' : '';
          return resolve({
            jsonrpc: '2.0',
            id: body && body.id != null ? body.id : null,
            result: {
              content: [{
                type: 'text',
                text: '这次调用需要付费 $' + price + '/次' + reason + '。\n' +
                  '二选一：\n' +
                  '1) 设置 PPC_API_KEY（预充值余额，按次扣费）\n' +
                  '2) 让客户端带 X-PAYMENT 头走 x402 链上按次支付\n' +
                  '可用工具：pricing.list（免费查价）、usage.report（免费对账）',
              }],
              isError: true,
              _billing: { charged: false, reason: 'payment_required', price_usd: price },
            },
          });
        }
        let parsed;
        try {
          parsed = JSON.parse(text);
        } catch (e) {
          return resolve({
            jsonrpc: '2.0',
            id: body && body.id != null ? body.id : null,
            error: { code: -32603, message: '网关返回非 JSON：' + text.slice(0, 200) },
          });
        }
        resolve(parsed);
      });
    });

    req.on('timeout', function () {
      req.destroy(new Error('网关响应超时'));
    });
    req.on('error', function (err) {
      resolve({
        jsonrpc: '2.0',
        id: body && body.id != null ? body.id : null,
        error: {
          code: -32000,
          message: '无法连接网关 ' + GATEWAY + '：' + err.message +
            '（先启动 python app.py，或把 PPC_GATEWAY 指向线上地址）',
        },
      });
    });
    req.write(payload);
    req.end();
  });
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });

// stdin 关闭后不能立刻退出：还有在途的 HTTP 请求没回来（脚本化调用常见）。
let pending = 0;
let closed = false;

function maybeExit() {
  if (closed && pending === 0) {
    setTimeout(function () { process.exit(0); }, 30);
  }
}

rl.on('line', function (line) {
  const text = line.trim();
  if (!text) return;
  let msg;
  try {
    msg = JSON.parse(text);
  } catch (e) {
    process.stdout.write(JSON.stringify({
      jsonrpc: '2.0', id: null,
      error: { code: -32700, message: 'Parse error' },
    }) + '\n');
    return;
  }
  log('->', text.slice(0, 200));
  pending += 1;
  post(msg).then(function (out) {
    if (out === null || out === undefined) return;   // 通知类消息无响应
    if (Array.isArray(out)) {
      out.filter(Boolean).forEach(function (o) {
        process.stdout.write(JSON.stringify(o) + '\n');
      });
      return;
    }
    process.stdout.write(JSON.stringify(out) + '\n');
  }).catch(function (err) {
    process.stdout.write(JSON.stringify({
      jsonrpc: '2.0', id: msg && msg.id != null ? msg.id : null,
      error: { code: -32603, message: String(err && err.message || err) },
    }) + '\n');
  }).then(function () {
    pending -= 1;
    maybeExit();
  });
});

rl.on('close', function () { closed = true; maybeExit(); });
process.on('SIGTERM', function () { process.exit(0); });
