import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 3456);
const API_BASE = 'https://api.topstepx.com';
const ENV_PATH = path.join(__dirname, '.env');
const LWC_BUNDLE_PATH = path.resolve(
	__dirname,
	'../lightweight-charts/dist/lightweight-charts.standalone.production.js'
);

const session = {
	token: null,
	userName: null,
	apiKey: null,
	live: false,
	connectedAt: null,
};

function loadEnvFile() {
	if (!fs.existsSync(ENV_PATH)) {
		return {};
	}
	const content = fs.readFileSync(ENV_PATH, 'utf8');
	const env = {};
	for (const rawLine of content.split(/\r?\n/)) {
		const line = rawLine.trim();
		if (!line || line.startsWith('#')) {
			continue;
		}
		const eqIdx = line.indexOf('=');
		if (eqIdx === -1) {
			continue;
		}
		const key = line.slice(0, eqIdx).trim();
		let val = line.slice(eqIdx + 1).trim();
		if (
			(val.startsWith('"') && val.endsWith('"')) ||
			(val.startsWith("'") && val.endsWith("'"))
		) {
			val = val.slice(1, -1);
		}
		env[key] = val;
	}
	return env;
}

function saveEnvFile(userName, apiKey, live) {
	const lines = [
		`TOPSTEPX_USERNAME=${userName}`,
		`TOPSTEPX_API_KEY=${apiKey}`,
		`TOPSTEPX_LIVE=${live ? 'true' : 'false'}`,
		'',
	];
	fs.writeFileSync(ENV_PATH, lines.join('\n'), 'utf8');
}

async function topstepPost(endpoint, payload, useAuth = true) {
	const headers = {
		'Accept': 'application/json, text/plain',
		'Content-Type': 'application/json',
	};
	if (useAuth) {
		if (!session.token) {
			throw new Error('NOT_AUTHENTICATED');
		}
		headers['Authorization'] = `Bearer ${session.token}`;
	}

	const res = await fetch(`${API_BASE}${endpoint}`, {
		method: 'POST',
		headers,
		body: JSON.stringify(payload),
	});

	const text = await res.text();
	let data;
	try {
		data = text ? JSON.parse(text) : {};
	} catch {
		throw new Error(`HTTP ${res.status}: ${text.slice(0, 300)}`);
	}

	if (!res.ok) {
		const msg = data?.errorMessage || data?.message || `HTTP ${res.status}`;
		throw new Error(msg);
	}

	if (data && data.success === false) {
		throw new Error(data.errorMessage || `TopstepX errorCode=${data.errorCode}`);
	}

	return data;
}

async function authenticate(userName, apiKey, live) {
	const data = await topstepPost(
		'/api/Auth/loginKey',
		{ userName, apiKey },
		false
	);
	if (!data.token) {
		throw new Error(data.errorMessage || 'Không nhận được token từ TopstepX.');
	}
	session.token = data.token;
	session.userName = userName;
	session.apiKey = apiKey;
	session.live = Boolean(live);
	session.connectedAt = new Date().toISOString();
	return data;
}

async function tryAutoConnectFromEnv() {
	const env = loadEnvFile();
	const userName = process.env.TOPSTEPX_USERNAME || env.TOPSTEPX_USERNAME;
	const apiKey = process.env.TOPSTEPX_API_KEY || env.TOPSTEPX_API_KEY;
	const live = (process.env.TOPSTEPX_LIVE || env.TOPSTEPX_LIVE) === 'true';
	if (userName && apiKey) {
		try {
			await authenticate(userName, apiKey, live);
			console.log(`Auto-connected to TopstepX as "${userName}" (live=${live})`);
		} catch (err) {
			console.warn(`Auto-connect from .env failed: ${err.message}`);
		}
	}
}

function sendJson(res, status, obj) {
	res.writeHead(status, {
		'Content-Type': 'application/json; charset=utf-8',
		'Cache-Control': 'no-store',
	});
	res.end(JSON.stringify(obj));
}

function readJsonBody(req) {
	return new Promise((resolve, reject) => {
		let body = '';
		req.on('data', chunk => {
			body += chunk;
			if (body.length > 1_000_000) {
				reject(new Error('Body too large'));
			}
		});
		req.on('end', () => {
			try {
				resolve(body ? JSON.parse(body) : {});
			} catch (err) {
				reject(err);
			}
		});
		req.on('error', reject);
	});
}

function pickBestContract(symbol, contracts) {
	if (!Array.isArray(contracts) || contracts.length === 0) {
		return null;
	}
	const upper = symbol.toUpperCase();
	const exactSymbol = contracts.filter(c => {
		const id = String(c.id || '').toUpperCase();
		const symId = String(c.symbolId || '').toUpperCase();
		const name = String(c.name || '').toUpperCase();
		return (
			symId.endsWith(`.${upper}`) ||
			id.includes(`.${upper}.`) ||
			name.startsWith(upper)
		);
	});
	const pool = exactSymbol.length > 0 ? exactSymbol : contracts;
	const active = pool.find(c => c.activeContract === true);
	return active || pool[0];
}

const server = http.createServer(async (req, res) => {
	const urlObj = new URL(req.url || '/', `http://localhost:${PORT}`);
	const pathname = urlObj.pathname;

	try {
		if (req.method === 'GET' && pathname === '/') {
			const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
			res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
			res.end(html);
			return;
		}

		if (req.method === 'GET' && pathname === '/vendor/lightweight-charts.js') {
			if (!fs.existsSync(LWC_BUNDLE_PATH)) {
				res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
				res.end('lightweight-charts bundle not found. Run pnpm build:prod first.');
				return;
			}
			res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8' });
			res.end(fs.readFileSync(LWC_BUNDLE_PATH));
			return;
		}

		if (req.method === 'GET' && pathname === '/api/status') {
			const env = loadEnvFile();
			sendJson(res, 200, {
				connected: Boolean(session.token),
				userName: session.userName,
				live: session.live,
				connectedAt: session.connectedAt,
				hasEnvCredentials: Boolean(env.TOPSTEPX_USERNAME && env.TOPSTEPX_API_KEY),
			});
			return;
		}

		if (req.method === 'POST' && pathname === '/api/connect') {
			const body = await readJsonBody(req);
			const userName = String(body.userName || '').trim();
			const apiKey = String(body.apiKey || '').trim();
			const live = Boolean(body.live);
			const saveToEnv = Boolean(body.saveToEnv);

			if (!userName || !apiKey) {
				sendJson(res, 400, { error: 'Vui lòng nhập đầy đủ Username và API Key.' });
				return;
			}

			await authenticate(userName, apiKey, live);
			if (saveToEnv) {
				saveEnvFile(userName, apiKey, live);
			}
			sendJson(res, 200, {
				connected: true,
				userName: session.userName,
				live: session.live,
				connectedAt: session.connectedAt,
			});
			return;
		}

		if (req.method === 'POST' && pathname === '/api/disconnect') {
			session.token = null;
			session.userName = null;
			session.apiKey = null;
			session.connectedAt = null;
			sendJson(res, 200, { connected: false });
			return;
		}

		if (req.method === 'GET' && pathname === '/api/contracts') {
			if (!session.token) {
				sendJson(res, 401, { error: 'Chưa kết nối TopstepX API.' });
				return;
			}
			const symbolsParam = urlObj.searchParams.get('symbols') || 'MES,MNQ,MYM,MGC';
			const liveParam = urlObj.searchParams.get('live');
			const live = liveParam !== null ? liveParam === 'true' : session.live;
			const symbols = symbolsParam
				.split(',')
				.map(s => s.trim().toUpperCase())
				.filter(Boolean);

			const results = {};
			for (const symbol of symbols) {
				try {
					const data = await topstepPost('/api/Contract/search', {
						live,
						searchText: symbol,
					});
					const contracts = Array.isArray(data.contracts) ? data.contracts : [];
					results[symbol] = {
						selected: pickBestContract(symbol, contracts),
						all: contracts,
					};
				} catch (err) {
					results[symbol] = {
						selected: null,
						all: [],
						error: err.message,
					};
				}
			}

			sendJson(res, 200, { live, contracts: results });
			return;
		}

		if (req.method === 'GET' && pathname === '/api/bars') {
			if (!session.token) {
				sendJson(res, 401, { error: 'Chưa kết nối TopstepX API.' });
				return;
			}
			const contractId = urlObj.searchParams.get('contractId');
			if (!contractId) {
				sendJson(res, 400, { error: 'Thiếu tham số contractId.' });
				return;
			}

			const unit = Number(urlObj.searchParams.get('unit') || 2);
			const unitNumber = Number(urlObj.searchParams.get('unitNumber') || 5);
			const limit = Math.min(20000, Math.max(10, Number(urlObj.searchParams.get('limit') || 500)));
			const days = Math.min(365, Math.max(1, Number(urlObj.searchParams.get('days') || 14)));
			const liveParam = urlObj.searchParams.get('live');
			const live = liveParam !== null ? liveParam === 'true' : session.live;

			const endTime = new Date();
			const startTime = new Date(endTime.getTime() - days * 24 * 60 * 60 * 1000);

			const data = await topstepPost('/api/History/retrieveBars', {
				contractId,
				live,
				startTime: startTime.toISOString(),
				endTime: endTime.toISOString(),
				unit,
				unitNumber,
				limit,
				includePartialBar: true,
			});

			const rawBars = Array.isArray(data.bars) ? data.bars : [];
			const byTime = new Map();
			for (const b of rawBars) {
				const ts = Math.floor(Date.parse(b.t) / 1000);
				if (!Number.isFinite(ts)) {
					continue;
				}
				byTime.set(ts, {
					time: ts,
					open: Number(b.o),
					high: Number(b.h),
					low: Number(b.l),
					close: Number(b.c),
					volume: Number(b.v ?? 0),
				});
			}

			const bars = Array.from(byTime.values()).sort((a, b) => a.time - b.time);
			sendJson(res, 200, { contractId, count: bars.length, bars });
			return;
		}

		res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
		res.end('Not found');
	} catch (err) {
		const status = err.message === 'NOT_AUTHENTICATED' ? 401 : 500;
		sendJson(res, status, { error: err.message || 'Internal Server Error' });
	}
});

await tryAutoConnectFromEnv();

server.listen(PORT, () => {
	console.log(`TopstepX Lightweight Charts viewer running at http://localhost:${PORT}/`);
});
