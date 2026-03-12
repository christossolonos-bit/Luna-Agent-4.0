const { Client } = require('discord.js');
const fs = require('fs');
const path = require('path');

// Read token from .env in project root
let token = 'paste-the-token-here';
try {
  const envPath = path.join(__dirname, '.env');
  if (fs.existsSync(envPath)) {
    const env = fs.readFileSync(envPath, 'utf8');
    const line = env.split('\n').find(l => l.startsWith('DISCORD_TOKEN='));
    if (line) token = line.split('=', 2)[1].trim().replace(/^["']|["']$/g, '');
  }
} catch (e) {
  console.log('Could not read .env:', e.message);
}

const client = new Client({ intents: [] });
client.login(token);
client.on('ready', () => console.log('Connected!'));
client.on('error', e => console.log('Error:', e));
