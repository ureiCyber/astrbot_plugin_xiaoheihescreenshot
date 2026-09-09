const fs = require("fs");

const source = fs.readFileSync("main.py", "utf8");
const markers = [
  'raw_items = await page.evaluate(r"""',
  'return await page.evaluate(r"""',
  'geometry = await page.evaluate(\n            """',
  '"""Expand the article body before lazy loading, cleanup and measurement."""\n        return await page.evaluate(r"""',
  '"""Scroll through the current article once so newly revealed media can load."""\n        await page.evaluate("""',
  '"""Hide page chrome and measure the bottom-most actually painted content."""\n        return await page.evaluate(r"""',
];

for (const marker of markers) {
  const markerIndex = source.indexOf(marker);
  if (markerIndex < 0) throw new Error(`Missing embedded script after ${marker}`);
  const start = markerIndex + marker.length;
  const end = source.indexOf('"""', start);
  if (end < 0) throw new Error(`Unterminated embedded script after ${marker}`);
  const script = source.slice(start, end);
  new Function(`return (${script});`);
  console.log(`Embedded JavaScript syntax OK: ${script.length} chars`);
}
