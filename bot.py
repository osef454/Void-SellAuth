import discord
import aiohttp
import asyncio
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────
DISCORD_TOKEN    = os.getenv("DISCORD_TOKEN")
SELLAUTH_API_KEY = os.getenv("SELLAUTH_API_KEY")
SHOP_ID          = os.getenv("SHOP_ID", "218070")
CHANNEL_ID       = int(os.getenv("CHANNEL_ID", "1481554434168328193"))
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL", "30"))   # secondes entre chaque check
# Statuts à notifier : "completed" | "completed,pending"
NOTIFY_STATUSES  = [s.strip().lower() for s in os.getenv("NOTIFY_STATUS", "completed").split(",")]

SELLAUTH_BASE    = "https://api.sellauth.com/v1"

# ── État interne (en mémoire) ────────────────────────────────────────────────
seen_ids: set[str] = set()
initialized       = False

# ── Discord ──────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
client  = discord.Client(intents=intents)


# ── Helpers ──────────────────────────────────────────────────────────────────
def status_color(status: str) -> discord.Color:
    match status.lower():
        case "completed": return discord.Color.green()
        case "pending":   return discord.Color.orange()
        case "expired":   return discord.Color.red()
        case _:           return discord.Color.blurple()

def status_emoji(status: str) -> str:
    match status.lower():
        case "completed": return "✅"
        case "pending":   return "⏳"
        case "expired":   return "❌"
        case _:           return "❓"


async def fetch_invoices(session: aiohttp.ClientSession) -> list[dict]:
    """Récupère toutes les factures (page 1 à N) du shop."""
    headers = {
        "Authorization": f"Bearer {SELLAUTH_API_KEY}",
        "Accept":        "application/json",
    }
    url     = f"{SELLAUTH_BASE}/shops/{SHOP_ID}/invoices"
    all_inv = []
    page    = 1

    while True:
        params = {"page": page, "per_page": 50}
        try:
            async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    print(f"[WARN] SellAuth API {resp.status}: {text[:200]}")
                    break
                body = await resp.json()
        except Exception as e:
            print(f"[ERROR] fetch_invoices: {e}")
            break

        # Gestion réponse paginée ou tableau brut
        if isinstance(body, dict):
            data      = body.get("data", [])
            last_page = body.get("last_page", 1)
        else:
            data      = body
            last_page = 1

        all_inv.extend(data)

        if page >= last_page:
            break
        page += 1

    return all_inv


def build_embed(invoice: dict) -> discord.Embed:
    status   = invoice.get("status", "unknown")
    inv_id   = invoice.get("id", "N/A")
    email    = invoice.get("email", "N/A")
    price    = invoice.get("price", "N/A")
    paid     = invoice.get("paid", None)
    currency = invoice.get("currency", "EUR")

    # Nom du/des produit(s) — plusieurs formats possibles selon l'API
    raw_products = invoice.get("products", invoice.get("product", ""))
    if isinstance(raw_products, list):
        product_name = ", ".join(
            p.get("name", str(p)) if isinstance(p, dict) else str(p)
            for p in raw_products
        )
    elif isinstance(raw_products, dict):
        product_name = raw_products.get("name", "N/A")
    else:
        product_name = str(raw_products) if raw_products else "N/A"

    # Méthode de paiement
    pm = invoice.get("payment_method", invoice.get("gateway", "N/A"))
    if isinstance(pm, dict):
        pm = pm.get("name", str(pm))

    created_at   = invoice.get("created_at", "N/A")
    completed_at = invoice.get("completed_at", None)

    symbol = "€" if str(currency).upper() in ("EUR", "€") else str(currency)

    embed = discord.Embed(
        title     = f"{status_emoji(status)}  Nouvelle vente — {status.capitalize()}",
        color     = status_color(status),
        timestamp = datetime.now(timezone.utc),
    )
    embed.add_field(name="🛒 Produit",     value=product_name or "N/A", inline=True)
    embed.add_field(name=f"💶 Prix",       value=f"{symbol}{price}",    inline=True)
    embed.add_field(name="💳 Paiement",    value=str(pm),               inline=True)
    embed.add_field(name="📧 Email",       value=str(email),            inline=True)
    embed.add_field(name="🕐 Créé le",     value=str(created_at),       inline=True)
    if completed_at:
        embed.add_field(name="✅ Complété le", value=str(completed_at),  inline=True)
    if paid is not None:
        embed.add_field(name="💰 Payé",    value="Oui" if paid else "Non", inline=True)
    embed.set_footer(text=f"Invoice ID: {inv_id}  •  Shop: {SHOP_ID}")
    return embed


# ── Boucle de polling ────────────────────────────────────────────────────────
async def poll_loop():
    global seen_ids, initialized

    await client.wait_until_ready()
    channel = client.get_channel(CHANNEL_ID)

    if channel is None:
        print(f"[ERROR] Salon introuvable : {CHANNEL_ID}  —  vérifiez CHANNEL_ID et les permissions.")
        return

    print(f"[✓] Polling SellAuth toutes les {POLL_INTERVAL}s  →  #{channel.name}")

    async with aiohttp.ClientSession() as session:
        while not client.is_closed():
            try:
                invoices = await fetch_invoices(session)

                if not initialized:
                    # Premier démarrage : on mémorise tout sans notifier
                    seen_ids = {inv["id"] for inv in invoices if "id" in inv}
                    initialized = True
                    print(f"[✓] Initialisation : {len(seen_ids)} facture(s) existante(s) ignorée(s).")
                else:
                    for inv in invoices:
                        inv_id = inv.get("id")
                        if not inv_id or inv_id in seen_ids:
                            continue

                        seen_ids.add(inv_id)
                        status = inv.get("status", "").lower()

                        if status in NOTIFY_STATUSES:
                            embed = build_embed(inv)
                            await channel.send(embed=embed)
                            print(f"[→] Notification envoyée : {inv_id}  ({status})")

            except Exception as e:
                print(f"[ERROR] poll_loop: {e}")

            await asyncio.sleep(POLL_INTERVAL)


# ── Events ───────────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    print(f"[✓] Connecté en tant que {client.user}  (id: {client.user.id})")
    await client.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="SellAuth Sales 💸"
        )
    )
    asyncio.ensure_future(poll_loop())


# ── Lancement ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN manquant dans les variables d'environnement.")
    if not SELLAUTH_API_KEY:
        raise ValueError("SELLAUTH_API_KEY manquant dans les variables d'environnement.")
    client.run(DISCORD_TOKEN)
