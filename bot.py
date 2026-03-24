import discord
import aiohttp
import asyncio
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN    = os.getenv("DISCORD_TOKEN")
SELLAUTH_API_KEY = os.getenv("SELLAUTH_API_KEY")
SHOP_ID          = os.getenv("SHOP_ID", "218070")
CHANNEL_ID       = int(os.getenv("CHANNEL_ID", "1481554434168328193"))
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL", "30"))
NOTIFY_STATUSES  = [s.strip().lower() for s in os.getenv("NOTIFY_STATUS", "completed,pending").split(",")]
PING_USER_ID     = int(os.getenv("PING_USER_ID", "1440399490229207151"))

SELLAUTH_BASE    = "https://api.sellauth.com/v1"

# ── État interne ─────────────────────────────────────────────────────────────
seen_ids: set[int]   = set()
products_cache: dict = {}  # {product_id: product_name}
initialized          = False

intents = discord.Intents.default()
client  = discord.Client(intents=intents)


def api_headers() -> dict:
    return {"Authorization": f"Bearer {SELLAUTH_API_KEY}", "Accept": "application/json"}


def unwrap(body) -> dict | list:
    """Si la réponse API est {'data': ...}, retourne le contenu de data."""
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


# ── API ──────────────────────────────────────────────────────────────────────
async def api_get(session: aiohttp.ClientSession, path: str, params: dict = None) -> dict | list | None:
    url = f"{SELLAUTH_BASE}/shops/{SHOP_ID}{path}"
    try:
        async with session.get(url, headers=api_headers(), params=params,
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                return await resp.json()
            print(f"[WARN] GET {path} → {resp.status}")
    except Exception as e:
        print(f"[ERROR] GET {path}: {e}")
    return None


async def fetch_invoices(session: aiohttp.ClientSession) -> list[dict]:
    all_inv, page = [], 1
    while True:
        body = await api_get(session, "/invoices", {"page": page, "per_page": 50})
        if body is None:
            break
        data = unwrap(body) if isinstance(body, dict) else body
        if isinstance(data, list):
            all_inv.extend(data)
        last_page = body.get("last_page", 1) if isinstance(body, dict) else 1
        if page >= last_page:
            break
        page += 1
    return all_inv


async def fetch_invoice_detail(session: aiohttp.ClientSession, invoice_id: int) -> dict:
    body = await api_get(session, f"/invoices/{invoice_id}")
    if body is None:
        return {}
    result = unwrap(body)
    return result if isinstance(result, dict) else {}


async def fetch_all_products(session: aiohttp.ClientSession) -> dict:
    cache, page = {}, 1
    while True:
        body = await api_get(session, "/products", {"page": page, "per_page": 100})
        if body is None:
            break
        data = unwrap(body) if isinstance(body, dict) else body
        if isinstance(data, list):
            for p in data:
                pid  = p.get("id")
                name = p.get("name") or p.get("title")
                if pid and name:
                    cache[int(pid)] = name
        last_page = body.get("last_page", 1) if isinstance(body, dict) else 1
        if page >= last_page:
            break
        page += 1
    print(f"[✓] Produits chargés : {len(cache)}  → {list(cache.values())}")
    return cache


async def fetch_product_name(session: aiohttp.ClientSession, pid: int) -> str:
    if pid in products_cache:
        return products_cache[pid]
    body = await api_get(session, f"/products/{pid}")
    if body:
        data = unwrap(body) if isinstance(body, dict) else body
        if isinstance(data, dict):
            name = data.get("name") or data.get("title")
            if name:
                products_cache[pid] = name
                return name
    return ""


# ── Extraction depuis items[] (doc officielle SellAuth) ──────────────────────
def extract_product_name(invoice: dict) -> str:
    """Récupère le nom du produit depuis items[].product.name."""
    items = invoice.get("items")
    if isinstance(items, list) and items:
        names = []
        for item in items:
            if isinstance(item, dict):
                prod = item.get("product")
                if isinstance(prod, dict):
                    n = prod.get("name")
                    if n and n not in names:
                        names.append(n)
        if names:
            return ", ".join(names)

    # Fallback : champ "product" au top-level (objet ou cache)
    product_val = invoice.get("product")
    if isinstance(product_val, dict):
        n = product_val.get("name") or product_val.get("title")
        if n:
            return n

    # Fallback : product_id dans le cache
    pid = invoice.get("product_id")
    if pid is not None:
        try:
            if int(pid) in products_cache:
                return products_cache[int(pid)]
        except (ValueError, TypeError):
            pass

    # Fallback : product_id depuis items
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                pid = item.get("product_id")
                if pid is not None:
                    try:
                        if int(pid) in products_cache:
                            return products_cache[int(pid)]
                    except (ValueError, TypeError):
                        pass

    return ""


def extract_deliverable(invoice: dict) -> str:
    """Récupère les clés/serials livrés depuis items[].delivered."""
    items = invoice.get("items")
    if not isinstance(items, list):
        return ""

    all_keys = []
    for item in items:
        if not isinstance(item, dict):
            continue
        delivered = item.get("delivered")
        if isinstance(delivered, list):
            for key in delivered:
                if key and isinstance(key, str):
                    all_keys.append(key)
        elif isinstance(delivered, str) and delivered.strip():
            all_keys.append(delivered.strip())

    return "\n".join(all_keys)


# ── Embed ────────────────────────────────────────────────────────────────────
CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}


def status_color(s: str) -> discord.Color:
    return {"completed": discord.Color.from_rgb(87, 242, 135),
            "pending":   discord.Color.from_rgb(255, 168, 0),
            "expired":   discord.Color.from_rgb(237, 66, 69)}.get(s, discord.Color.blurple())


def status_label(s: str) -> str:
    return {"completed": "✅  Vente complétée",
            "pending":   "⏳  Paiement en attente",
            "expired":   "❌  Facture expirée"}.get(s, f"❓  {s.capitalize()}")


def format_date(raw) -> str:
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt.strftime("%d/%m/%Y à %H:%M")
    except Exception:
        return str(raw) if raw else "N/A"


def build_embed(invoice: dict, product_name: str) -> discord.Embed:
    status   = invoice.get("status", "unknown").lower()
    inv_id   = invoice.get("id", "N/A")
    email    = invoice.get("email", "N/A")
    price    = invoice.get("price", "0.00")
    currency = str(invoice.get("currency", "EUR")).upper()
    symbol   = CURRENCY_SYMBOLS.get(currency, currency)
    is_paid  = status == "completed"

    pm = invoice.get("payment_method") or invoice.get("gateway", "N/A")
    if isinstance(pm, dict):
        pm = pm.get("name") or "N/A"

    deliverable = extract_deliverable(invoice)

    embed = discord.Embed(
        title       = status_label(status),
        description = f"```\nShop ID : {SHOP_ID}   •   Invoice : {inv_id}\n```",
        color       = status_color(status),
        timestamp   = datetime.now(timezone.utc),
    )
    embed.add_field(name="🛒  Produit",  value=f"**{product_name}**",                    inline=False)
    embed.add_field(name="💶  Prix",     value=f"**{symbol}{price}**",                   inline=True)
    embed.add_field(name="💳  Paiement", value=f"**{pm}**",                              inline=True)
    embed.add_field(name="💰  Payé",     value="✅ **Oui**" if is_paid else "❌ **Non**", inline=True)
    embed.add_field(name="📧  Email",    value=f"`{email}`",                             inline=True)
    embed.add_field(name="🕐  Créé le",  value=format_date(invoice.get("created_at")),   inline=True)

    completed_at = invoice.get("completed_at")
    if completed_at:
        embed.add_field(name="✅  Complété le", value=format_date(completed_at), inline=True)

    if deliverable:
        display = deliverable if len(deliverable) <= 1000 else deliverable[:997] + "..."
        embed.add_field(name="🔑  Clé / Deliverable", value=f"```\n{display}\n```", inline=False)

    embed.set_footer(text="Void SellAuth  •  Nouvelle transaction")
    return embed


# ── Résolution complète du produit ───────────────────────────────────────────
async def resolve_product(session: aiohttp.ClientSession, invoice: dict) -> str:
    name = extract_product_name(invoice)
    if name:
        return name

    # Dernier recours : appel API produit individuel via items[].product_id
    items = invoice.get("items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                pid = item.get("product_id")
                if pid:
                    n = await fetch_product_name(session, int(pid))
                    if n:
                        return n

    return "N/A"


# ── Polling ──────────────────────────────────────────────────────────────────
async def poll_loop():
    global seen_ids, initialized, products_cache

    await client.wait_until_ready()
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        print(f"[ERROR] Salon introuvable : {CHANNEL_ID}")
        return

    print(f"[✓] Polling toutes les {POLL_INTERVAL}s  →  #{channel.name}")

    async with aiohttp.ClientSession() as session:
        products_cache = await fetch_all_products(session)

        while not client.is_closed():
            try:
                invoices = await fetch_invoices(session)

                if not initialized:
                    seen_ids    = {inv["id"] for inv in invoices if "id" in inv}
                    initialized = True
                    print(f"[✓] Init : {len(seen_ids)} facture(s) existante(s).")
                else:
                    for inv in invoices:
                        inv_id = inv.get("id")
                        if not inv_id or inv_id in seen_ids:
                            continue
                        seen_ids.add(inv_id)

                        status = inv.get("status", "").lower()
                        if status not in NOTIFY_STATUSES:
                            continue

                        # Détail complet de la facture (déplie "data" si nécessaire)
                        detail = await fetch_invoice_detail(session, inv_id)
                        full   = {**inv, **detail} if detail else inv

                        # Résoudre le produit par tous les moyens
                        product_name = await resolve_product(session, full)

                        embed = build_embed(full, product_name)
                        content = f"<@{PING_USER_ID}>" if status == "completed" else None
                        await channel.send(content=content, embed=embed)
                        print(f"[→] {inv_id}  ({status})  produit: {product_name}")

            except Exception as e:
                print(f"[ERROR] poll_loop: {e}")

            await asyncio.sleep(POLL_INTERVAL)


# ── Events ───────────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    print(f"[✓] {client.user}  (id: {client.user.id})")
    await client.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="SellAuth Sales 💸")
    )
    asyncio.ensure_future(poll_loop())


# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN manquant.")
    if not SELLAUTH_API_KEY:
        raise ValueError("SELLAUTH_API_KEY manquant.")
    client.run(DISCORD_TOKEN)
