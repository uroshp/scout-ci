// Keep the Streamlit Community Cloud viewer awake.
//
// Streamlit sleeps any app with no traffic for ~12h. A plain HTTP GET returns 200
// but does NOT wake it — the awake/asleep state is tied to a real browser WEBSOCKET
// session, not the request. So we open the app in a real headless browser. If it is
// asleep, Streamlit shows a "Yes, get this app back up!" button; we click it and wait
// for the app to boot. Either way the visit registers as genuine traffic and resets
// the 12h timer.
//
// Exits non-zero on failure so a broken run shows up RED in the Actions tab.
import { chromium } from 'playwright';

const URL = process.env.APP_URL;
const WAKE = /get this app back up/i;

const browser = await chromium.launch();
const page = await browser.newPage();
try {
  await page.goto(URL, { waitUntil: 'networkidle', timeout: 60_000 });

  // The wake button only exists on the sleeping page. Try it as a button first, then
  // fall back to plain text, since Streamlit's markup can change.
  let wake = page.getByRole('button', { name: WAKE });
  if (!(await wake.count())) wake = page.getByText(WAKE);

  if (await wake.count()) {
    console.log('App was ASLEEP — clicking the wake button.');
    await wake.first().click();
    await page.waitForLoadState('networkidle', { timeout: 120_000 });
    await page.waitForTimeout(15_000); // let it finish spinning up
  } else {
    console.log('App was already awake.');
  }

  // Hold the session open a moment so the websocket visit counts as real traffic.
  await page.waitForTimeout(10_000);
  console.log('Done. Page title:', await page.title());
} finally {
  await browser.close();
}
