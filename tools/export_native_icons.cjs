// Keep the native icon catalog identical to the existing web icon library.
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const root = path.resolve(__dirname, '..');
const context = {window:{}};
vm.runInNewContext(fs.readFileSync(path.join(root, 'web/brand-icons.js'), 'utf8'), context);
fs.writeFileSync(path.join(root, 'web/assets/native-icons.json'), JSON.stringify(context.window.ExplorerIcons, null, 2)+'\n');
