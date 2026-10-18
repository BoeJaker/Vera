const fs = require('fs');
const path = require('path');

const panel = fs.readFileSync(path.join(__dirname, '..', 'vera', 'intel', 'intel_panel.html'), 'utf8');

function expect(pattern, message) {
  if (!pattern.test(panel)) throw new Error(message);
}

expect(/How Intelligence connects/, 'activity pane must explain shared evidence');
expect(/call\('intel\.models'\)/, 'panel must read the model-consumer projection');
expect(/call\('intel\.effects'\)/, 'panel must read owning-effect evidence');
expect(/call\('intel\.records', \{limit: 200\}\)/, 'panel must use bounded record reconciliation');
expect(/The Intel queue still executes and controls them/, 'panel must state native execution authority');
expect(/enforcement remains off/, 'panel must not imply effect enforcement');
expect(/Memory delivery has no projection identity/, 'panel must expose the projection evidence gap');

for (const internal of ['INTEL-D', 'ADOPT-', 'W0-', 'W1-', 'W2-', 'W3-', 'W4-', 'W5-', 'W6-']) {
  if (panel.includes(internal)) throw new Error(`internal roadmap label leaked into product copy: ${internal}`);
}

console.log('intel evidence panel contract ok');
