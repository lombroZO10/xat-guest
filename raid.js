const {
    chromium
} = require('playwright');
const fs = require('fs');
const path = require('path');

const PROXY_FILE = path.join(__dirname, 'proxies.txt');
const MAX_CONCURRENT_CONTEXTS = 200; // Limit the number of concurrent contexts

// Function to read proxies from a file
function readProxiesFromFile(filePath) {
    return new Promise((resolve, reject) => {
        fs.readFile(filePath, 'utf8', (err, data) => {
            if (err) {
                reject(err);
            } else {
                const proxies = data.split('\n').map(line => line.trim()).filter(line => line);
                resolve(proxies);
            }
        });
    });
}

// Function to handle each proxy
async function handleProxy(browser, proxy) {
    let context;
    let page;
    try {
        context = await browser.newContext({
            proxy: {
                server: `http://${proxy}`,
            },
        });
        page = await context.newPage();
        await page.goto('https://xat.com/content/web/R00188/box/embed.html?n=aff');
        const title = await page.title();

        if (title !== 'xat') {
            await page.close();
        }
        // Get all frames on the page
        const frames = await page.frames();


        const innerFrame = await frames.find(frame => frame.name() === 'appframe');

        // Perform actions on the element within the inner frame
        const textEntryEditable = innerFrame.locator('#textEntryEditable');

        await textEntryEditable.click();
        await textEntryEditable.fill("RAID 2024");
        await textEntryEditable.press('Enter');
        console.log(`Proxy: ${proxy} - ${title}`);

    } catch (error) {
        //console.error(`Error with proxy:`, error);
        //await browser.close();
    } finally {

    }
}

// Main function to run the script
async function main() {
    let browser;
    try {
        const proxies = await readProxiesFromFile(PROXY_FILE);
        browser = await chromium.launch({
            headless: true,
            args: ['--no-sandbox', '--disable-setuid-sandbox']
        });

        // Process proxies in chunks
        for (let i = 0; i < proxies.length; i += MAX_CONCURRENT_CONTEXTS) {
            const chunk = proxies.slice(i, i + MAX_CONCURRENT_CONTEXTS);
            const chunkPromises = chunk.map(proxy => handleProxy(browser, proxy));
            await Promise.all(chunkPromises);
        }

    } catch (error) {
        console.error('Error occurred:', error);
    } finally {
        if (browser) await browser.close();
    }
}

main();