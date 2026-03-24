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

SELLAUTH_BASE    = "https://api.sellauth.com/v1"
HEADERS          = lambda: {"Authorization": f"Bearer {SELLAUTH_API_KEY}", "Accept": "application/json"}

# ── État interne ─────────────────────────────────────────────────────────────
seen_ids: set[int]      = set()
products_cache: dict    = {}   # product_id (int) -> name (str)
initialized             = False

intents = discord.Intents.default()
client  = discord.Client(intents=intents)


# ── API helpers ───────────────────────────────────────────────────────────────
async def fetch_invoices(session: aiohttp.ClientSession) -> list[dict]:
    url, all_inv, page = f"{SELLAUTH_BASE}/shops/{SHOP_ID}/invoices", [], 1
    while True:
        try:
            async with session.get(url, headers=HEADERS(), params={"page": page, "per_page": 50},
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    print(f"[WARN] invoices list {resp.status}: {(await resp.text())[:200]}")
                    break
                body = await resp.json()
        except Exception as e:
            print(f"[ERROR] fetch_invoices: {e}"); break

        data      = body.get("data", body) if isinstance(body, dict) else body
        last_page = body.get("last_page", 1) if isinstance(body, dict) else 1
        all_inv.extend(data)
        if page >= last_page:
            break
        page += 1
    return all_inv


async def fetch_invoice_detail(session: aiohttp.ClientSession, invoice_id: int) -> dict:
    """Récupère les détails complets d'une facture (contient le produit)."""
    url = f"{SELLAUTH_BASE}/shops/{SHOP_ID}/invoices/{invoice_id}"
    try:
        async with session.get(url, headers=HEADERS(), timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                return await resp.json()
            print(f"[WARN] invoice detail {invoice_id} → {resp.status}")
    except Exception as e:
        print(f"[ERROR] fetch_invoice_detail {invoice_id}: {e}")
    return {}


async def fetch_all_products(session: aiohttp.ClientSession) -> dict:
    """Charge tous les produits du shop → dict {product_id: name}."""
    url    = f"{SELLAUTH_BASE}/shops/{SHOP_ID}/products"
    cache  = {}
    page   = 1
    while True:
        try:
            async with session.get(url, headers=HEADERS(), params={"page": page, "per_page": 100},
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    break
                body = await resp.json()
        except Exception as e:
            print(f"[ERROR] fetch_all_products: {e}"); break

        data      = body.get("data", body) if isinstance(body, dict) else body
        last_page = body.get("last_page", 1) if isinstance(body, dict) else 1
        for p in data:
            pid  = p.get("id")
            name = p.get("name") or p.get("title") or p.get("label")
            if pid and name:
                cache[int(pid)] = name
        if page >= last_page:
            break
        page += 1
    print(f"[✓] Produits chargés : {len(cache)}  → {list(cache.values())}")
    return cache


async def fetch_product_name(session: aiohttp.ClientSession, product_id: int) -> str:
    """Récupère le nom d'un produit par son ID (avec mise en cache)."""
    if product_id in products_cache:
        return products_cache[product_id]
    url = f"{SELLAUTH_BASE}/shops/{SHOP_ID}/products/{product_id}"
    try:
        async with session.get(url, headers=HEADERS(), timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                data = await resp.json()
                name = data.get("name") or data.get("title") or data.get("label")
                if name:
                    products_cache[product_id] = name
                    return name
    except Exception as e:
        print(f"[ERROR] fetch_product_name {product_id}: {e}")
    return ""


# ── Extraction produit ────────────────────────────────────────────────────────
def extract_product_name(invoice: dict) -> str:
    # 1. Champs plats directs
    for key in ("product_title", "product_name", "title", "name"):
        v = invoice.get(key)
        if v and isinstance(v, str):
            return v

    # 2. Objet "product" ou liste "products"
    raw = invoice.get("products") or invoice.get("product")
    if isinstance(raw, list) and raw:
        parts = []
        for p in raw:
            if isinstance(p, dict):
                n = p.get("name") or p.get("title") or p.get("label")
            else:
                n = str(p)
            if n: parts.append(n)
        return ", ".join(parts) or ""
    if isinstance(raw, dict):
        return raw.get("name") or raw.get("title") or raw.get("label") or ""
    if isinstance(raw, str) and raw:
        return raw

    # 3. Lookup dans le cache via product_id
    pid = invoice.get("product_id")
    if not pid and isinstance(invoice.get("product"), dict):
        pid = invoice["product"].get("id")
    if pid and int(pid) in products_cache:
        return products_cache[int(pid)]

    return ""


# ── Build embed ───────────────────────────────────────────────────────────────
CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}

def status_color(s: str) -> discord.Color:
    return {"completed": discord.Color.from_rgb(87, 242, 135),
            "pending":   discord.Color.from_rgb(255, 168, 0),
            "expired":   discord.Color.from_rgb(237, 66, 69)}.get(s.lower(), discord.Color.blurple())

def status_label(s: str) -> str:
    return {"completed": "✅  Vente complétée", "pending": "⏳  Paiement en attente",
            "expired":   "❌  Facture expirée"}.get(s.lower(), f"❓  {s.capitalize()}")


def build_embed(invoice: dict) -> discord.Embed:
    status       = invoice.get("status", "unknown").lower()
    inv_id       = invoice.get("id", "N/A")
    email        = invoice.get("email", "N/A")
    price        = invoice.get("price", "0.00")
    currency     = str(invoice.get("currency", "EUR")).upper()
    created_raw  = invoice.get("created_at", "")
    completed_at = invoice.get("completed_at")

    # Formatage date
    try:
        dt = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
        created_fmt = dt.strftime("%d/%m/%Y à %H:%M")
    except Exception:
        created_fmt = str(created_raw)

    symbol       = CURRENCY_SYMBOLS.get(currency, currency)
    product_name = invoice.get("_resolved_product") or extract_product_name(invoice) or "N/A"
    is_paid      = status == "completed"

    # Méthode de paiement
    pm = invoice.get("payment_method") or invoice.get("gateway", "N/A")
    if isinstance(pm, dict):
        pm = pm.get("name") or "N/A"

    embed = discord.Embed(
        title       = status_label(status),
        description = f"```\nShop ID : {SHOP_ID}   •   Invoice : {inv_id}\n```",
        color       = status_color(status),
        timestamp   = datetime.now(timezone.utc),
    )

    embed.add_field(name="🛒  Produit",      value=f"**{product_name}**",                    inline=False)
    embed.add_field(name="💶  Prix",          value=f"**{symbol}{price}**",                   inline=True)
    embed.add_field(name="💳  Paiement",      value=f"**{pm}**",                              inline=True)
    embed.add_field(name="💰  Payé",          value="✅ **Oui**" if is_paid else "❌ **Non**", inline=True)
    embed.add_field(name="📧  Email",         value=f"`{email}`",                             inline=True)
    embed.add_field(name="🕐  Créé le",       value=created_fmt,                              inline=True)

    if completed_at:
        try:
            dt2 = datetime.fromisoformat(str(completed_at).replace("Z", "+00:00"))
            completed_fmt = dt2.strftime("%d/%m/%Y à %H:%M")
        except Exception:
            completed_fmt = str(completed_at)
        embed.add_field(name="✅  Complété le", value=completed_fmt, inline=True)

    embed.set_footer(
        text    = "Void SellAuth  •  Nouvelle transaction",
        icon_url= "https://sellauth.com/favicon.ico",
    )
    return embed


# ── Polling loop ──────────────────────────────────────────────────────────────
async def poll_loop():
    global seen_ids, initialized

    await client.wait_until_ready()
    channel = client.get_channel(CHANNEL_ID)

    if channel is None:
        print(f"[ERROR] Salon introuvable : {CHANNEL_ID}")
        return

    print(f"[✓] Polling toutes les {POLL_INTERVAL}s  →  #{channel.name}")

    async with aiohttp.ClientSession() as session:
        # Pré-chargement du catalogue produits
        global products_cache
        products_cache = await fetch_all_products(session)

        while not client.is_closed():
            try:
                invoices = await fetch_invoices(session)

                if not initialized:
                    seen_ids    = {inv["id"] for inv in invoices if "id" in inv}
                    initialized = True
                    print(f"[✓] Init : {len(seen_ids)} facture(s) existante(s) ignorée(s).")
                else:
                    for inv in invoices:
                        inv_id = inv.get("id")
                        if not inv_id or inv_id in seen_ids:
                            continue

                        seen_ids.add(inv_id)
                        status = inv.get("status", "").lower()

                        if status in NOTIFY_STATUSES:
                            # Appel détaillé pour récupérer tous les champs
                            detail = await fetch_invoice_detail(session, inv_id)
                            full   = {**inv, **detail} if detail else inv

                            # Si produit encore inconnu → lookup par product_id
                            product_name = extract_product_name(full)
                            if not product_name:
                                pid = full.get("product_id")
                                if not pid and isinstance(full.get("product"), dict):
                                    pid = full["product"].get("id")
                                if pid:
                                    product_name = await fetch_product_name(session, int(pid))

                            # Injecte le nom résolu dans le dict pour l'embed
                            if product_name:
                                full["_resolved_product"] = product_name

                            embed = build_embed(full)
                            await channel.send(embed=embed)
                            print(f"[→] {inv_id}  ({status})  produit: {product_name or 'N/A'}")

            except Exception as e:
                print(f"[ERROR] poll_loop: {e}")

            await asyncio.sleep(POLL_INTERVAL)


# ── Events ────────────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    print(f"[✓] {client.user}  (id: {client.user.id})")
    await client.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="SellAuth Sales 💸")
    )
    asyncio.ensure_future(poll_loop())


# ── Lancement ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN manquant.")
    if not SELLAUTH_API_KEY:
        raise ValueError("SELLAUTH_API_KEY manquant.")
    client.run(DISCORD_TOKEN)
