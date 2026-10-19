const fs = require('fs');
const file = process.argv[2];
const src = fs.readFileSync(file, 'utf8');

const CUSTOM = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+/=';
const STD = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=';

function b64decode(s) {
    let remap = '';
    for (const c of s) {
        const idx = CUSTOM.indexOf(c);
        remap += idx >= 0 ? STD[idx] : c;
    }
    return Buffer.from(remap, 'base64');
}

function rc4(data, key) {
    const S = Array.from({ length: 256 }, (_, i) => i);
    let j = 0;
    for (let i = 0; i < 256; i++) {
        j = (j + S[i] + key.charCodeAt(i % key.length)) % 256;
        [S[i], S[j]] = [S[j], S[i]];
    }
    let a = 0; j = 0;
    const out = Buffer.alloc(data.length);
    for (let k = 0; k < data.length; k++) {
        a = (a + 1) % 256;
        j = (j + S[a]) % 256;
        [S[a], S[j]] = [S[j], S[a]];
        out[k] = data[k] ^ S[(S[a] + S[j]) % 256];
    }
    return out;
}

function cleanText(b) {
    if (b.length === 0) return false;
    for (const x of b) {
        if (x < 0x20 || x > 0x7e) return false; // so ASCII imprimivel
    }
    return true;
}

// todas as arrays _0x... = ['...']
const arrays = [];
const arrRe = /(_0x[0-9a-f]{4,})\s*=\s*(\[[\s\S]*?\])\s*[;,]/g;
let am;
while ((am = arrRe.exec(src)) !== null) {
    try {
        const arr = eval(am[2]);
        if (Array.isArray(arr) && arr.length > 3) arrays.push({ name: am[1], vals: arr });
    } catch (e) {}
}
console.log('arrays:', arrays.length, arrays.map(a => a.name + ':' + a.vals.length).join(' '));

// candidatos a chave: literais de string curtos
const keySet = new Set();
const litRe = /'([^'\\]{1,20})'|"([^"\\]{1,20})"/g;
let lm;
while ((lm = litRe.exec(src)) !== null) {
    const k = lm[1] || lm[2];
    if (k && k.length >= 1 && k.length <= 20) keySet.add(k);
}
const keys = [...keySet];

const decoded = [];
for (const arr of arrays) {
    for (const s of arr.vals) {
        let done = false;
        for (const key of keys) {
            try {
                const raw = b64decode(s);
                const plain = rc4(raw, key);
                if (cleanText(plain)) {
                    decoded.push(plain.toString('utf8'));
                    done = true;
                    break;
                }
            } catch (e) {}
        }
        if (!done) decoded.push('<' + s.slice(0, 16) + '>');
    }
}

fs.writeFileSync(file + '.decoded.txt', decoded.join('\n'));
console.log('total decodificados:', decoded.length);
const hit = decoded.filter(s => /l5|perlin|noise|wss|ws:|socket|chat|web_gear|room|v2|auser|token|\.js|http|\.php|json|proto|binary|ArrayBuffer/i.test(s));
console.log('\n=== ALVOS ===');
console.log([...new Set(hit)].slice(0, 200).join('\n'));
