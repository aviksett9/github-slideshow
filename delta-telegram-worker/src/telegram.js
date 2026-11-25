// Tiny Telegram Bot API client.

export function escapeHtml(value) {
  return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

const MAX_MESSAGE = 4096;

export class Telegram {
  constructor(token) {
    this.token = token;
  }

  async call(method, params) {
    const res = await fetch(`https://api.telegram.org/bot${this.token}/${method}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    });
    let data = null;
    try {
      data = await res.json();
    } catch {
      // ignore
    }
    if (!data?.ok) {
      // Never include the URL here: it contains the bot token.
      throw new Error(`Telegram ${method} failed: ${data?.description || `HTTP ${res.status}`}`);
    }
    return data.result;
  }

  sendMessage(chatId, text, extra = {}) {
    const body = text.length > MAX_MESSAGE ? `${text.slice(0, MAX_MESSAGE - 1)}…` : text;
    return this.call('sendMessage', {
      chat_id: chatId,
      text: body,
      parse_mode: 'HTML',
      link_preview_options: { is_disabled: true },
      ...extra,
    });
  }
}
