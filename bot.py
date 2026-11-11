"""Discord sales-log reporter.

Reads only the configured log channels and reports sales grouped by customer name.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()


LOCAL_TZ = ZoneInfo(os.getenv("TIMEZONE", "America/Santiago"))
LOG_CHANNEL_IDS = {
    int(value.strip())
    for value in os.getenv("LOG_CHANNEL_IDS", "").split(",")
    if value.strip()
}
HOURS_CHANNEL_IDS = {
    int(value.strip())
    for value in os.getenv(
        "HOURS_CHANNEL_IDS",
        os.getenv("LOG_CHANNEL_IDS", ""),
    ).split(",")
    if value.strip()
}
REPORT_ROLE_IDS = {
    int(value.strip())
    for value in os.getenv("REPORT_ROLE_IDS", "").split(",")
    if value.strip()
}

NAME_RE = re.compile(
    r"Nombre\s*:\s*(?P<name>.+?)"
    r"(?=\s*(?:Total\s+pagado\s+por\s+el\s+cliente|Comisi[oó]n\s+del\s+empleado|Ingresos\s+del\s+negocio|Productos)\s*:)",
    re.IGNORECASE | re.DOTALL,
)
TOTAL_RE = re.compile(
    r"Total\s+pagado\s+por\s+el\s+cliente\s*:\s*\$?\s*([\d.,]+)",
    re.IGNORECASE,
)
SERVICE_NAME_RE = re.compile(
    r"Nombre\s*:\s*(?P<name>.+?)"
    r"(?=\s*(?:Identificador|ID)\s*:|$)",
    re.IGNORECASE | re.DOTALL,
)
SERVICE_ID_RE = re.compile(
    r"(?:Identificador|ID)\s*:\s*(?P<identifier>[^\s\r\n]+)",
    re.IGNORECASE,
)


def parse_amount(raw: str) -> int:
    # These logs use whole pesos and may include thousands separators.
    return int(re.sub(r"[^0-9]", "", raw))


def sale_text(message: discord.Message) -> str:
    parts = [message.content or ""]
    for embed in message.embeds:
        parts.extend([embed.title or "", embed.description or ""])
        for field in embed.fields:
            parts.extend([field.name or "", field.value or ""])
    return "\n".join(parts)


def parse_sale(message: discord.Message) -> tuple[str, int] | None:
    # Embed fields may split a label and its value across separate lines;
    # Markdown bold markers are presentation only and should not affect parsing.
    text = re.sub(r"\*{1,2}", "", sale_text(message))
    name_match = NAME_RE.search(text)
    total_match = TOTAL_RE.search(text)
    if not name_match or not total_match:
        return None
    name = " ".join(name_match.group("name").split())
    if not name:
        return None
    return name, parse_amount(total_match.group(1))


def parse_service_event(
    message: discord.Message,
) -> tuple[str, str, str, datetime] | None:
    """Return event kind, worker name, identifier, and timestamp."""
    text = re.sub(r"\*{1,2}", "", sale_text(message))
    normalized = text.casefold()

    if "entrada de servicio" in normalized or "entrada en servicio" in normalized:
        kind = "entrada"
    elif "salida de servicio" in normalized or "salida en servicio" in normalized:
        kind = "salida"
    else:
        return None

    name_match = SERVICE_NAME_RE.search(text)
    if not name_match:
        return None

    name = " ".join(name_match.group("name").split())
    if not name:
        return None

    id_match = SERVICE_ID_RE.search(text)
    identifier = id_match.group("identifier").strip() if id_match else name.casefold()
    return kind, name, identifier, message.created_at


def format_pesos(value: int) -> str:
    return f"${value:,}".replace(",", ".")


def render_table(header: str, rows: list[str]) -> str:
    """Render an aligned table with a Unicode frame for Discord."""
    width = len(header)
    top = f"┌{'─' * (width + 2)}┐"
    middle = f"├{'─' * (width + 2)}┤"
    bottom = f"└{'─' * (width + 2)}┘"
    rendered = [top, f"│ {header} │", middle]

    for row in rows:
        if row and set(row) == {"-"}:
            rendered.append(middle)
        else:
            rendered.append(f"│ {row:<{width}} │")

    rendered.append(bottom)
    return "\n".join(rendered)


class SalesBot(commands.Bot):
    async def setup_hook(self) -> None:
        await self.tree.sync()


intents = discord.Intents.default()
intents.message_content = True
bot = SalesBot(command_prefix="!", intents=intents)


@bot.tree.command(name="ventas", description="Resume ventas de los canales de logs")
@app_commands.describe(
    desde="Fecha inicial inclusiva (AAAA-MM-DD)",
    hasta="Fecha final inclusiva (AAAA-MM-DD)",
)
async def ventas(interaction: discord.Interaction, desde: str, hasta: str) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Este comando funciona dentro del servidor.", ephemeral=True)
        return
    if not LOG_CHANNEL_IDS:
        await interaction.response.send_message("Falta configurar LOG_CHANNEL_IDS en el bot.", ephemeral=True)
        return
    if REPORT_ROLE_IDS and not any(
        role.id in REPORT_ROLE_IDS for role in getattr(interaction.user, "roles", [])
    ):
        await interaction.response.send_message("No tienes permiso para generar este informe.", ephemeral=True)
        return

    try:
        first = date.fromisoformat(desde)
        last = date.fromisoformat(hasta)
        if last < first:
            raise ValueError
    except ValueError:
        await interaction.response.send_message(
            "Usa fechas válidas en formato AAAA-MM-DD y asegúrate de que 'hasta' no sea anterior a 'desde'.",
            ephemeral=True,
        )
        return

    start_local = datetime.combine(first, time.min, tzinfo=LOCAL_TZ)
    end_local = datetime.combine(last + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    await interaction.response.defer(thinking=True, ephemeral=True)

    totals: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    canonical: dict[str, str] = {}
    scanned = 0
    matched = 0
    skipped_channels: list[int] = []

    for channel_id in LOG_CHANNEL_IDS:
        channel = interaction.guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "history"):
            skipped_channels.append(channel_id)
            continue
        try:
            async for message in channel.history(
                limit=None, after=start_utc - timedelta(seconds=1),
                before=end_utc, oldest_first=True,
            ):
                scanned += 1
                parsed = parse_sale(message)
                if parsed is None:
                    continue
                name, total = parsed
                key = name.casefold()
                canonical.setdefault(key, name)
                totals[key] += total
                counts[key] += 1
                matched += 1
        except (discord.Forbidden, discord.HTTPException):
            skipped_channels.append(channel_id)

    if matched == 0:
        await interaction.followup.send(
            f"No encontré registros de venta entre {first.isoformat()} y {last.isoformat()}. "
            f"Revisé {scanned} mensajes.", ephemeral=True,
        )
        return

    rows = sorted(totals, key=lambda key: (-totals[key], canonical[key].casefold()))
    total_all = sum(totals.values())
    payout_all = total_all * 30 // 100
    name_width = min(max(len("Jugador"), *(len(canonical[key]) for key in rows)), 30)
    sales_width = max(len("Ventas"), len(str(max(counts.values()))))
    sold_width = max(len("Total vendido"), *(len(format_pesos(totals[key])) for key in rows))
    pay_width = max(len("30% a pagar"), *(len(format_pesos(totals[key] * 30 // 100)) for key in rows))

    def table_row(name: str, sale_count: int, sold: int, payout: int) -> str:
        display_name = name if len(name) <= name_width else name[: name_width - 3] + "..."
        return (
            f"{display_name:<{name_width}}  {sale_count:>{sales_width}}  "
            f"{format_pesos(sold):>{sold_width}}  {format_pesos(payout):>{pay_width}}"
        )

    header_row = (
        f"{'Jugador':<{name_width}}  {'Ventas':>{sales_width}}  "
        f"{'Total vendido':>{sold_width}}  {'30% a pagar':>{pay_width}}"
    )
    separator = "-" * len(header_row)
    data_rows = [table_row(canonical[key], counts[key], totals[key], totals[key] * 30 // 100) for key in rows]
    heading = f"Ventas · {first.isoformat()} a {last.isoformat()}"

    # Keep each table inside Discord's 2,000-character message limit.
    table_pages: list[list[str]] = []
    page: list[str] = []
    for row in data_rows:
        candidate = page + [row]
        if len("\n".join([heading, "```text", header_row, separator, *candidate, "```"])) > 1850 and page:
            table_pages.append(page)
            page = [row]
        else:
            page = candidate
    if page:
        table_pages.append(page)

    for page_index, page_rows in enumerate(table_pages):
        final_page = page_index == len(table_pages) - 1
        footer = [separator, table_row("TOTAL", matched, total_all, payout_all)] if final_page else []
        if skipped_channels and final_page:
            footer.append(f"\nNo pude leer {len(skipped_channels)} canal(es); verifica permisos del bot.")
        embed = discord.Embed(
            title=f"📊 {heading}",
            description=f"```text\n{render_table(header_row, page_rows + footer)}\n```",
            color=discord.Color.from_rgb(46, 204, 113),
        )
        embed.set_footer(
            text=f"{LOCAL_TZ.key} · Comisión calculada al 30% del total pagado"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="horas", description="Calcula horas trabajadas y pago por trabajador")
@app_commands.describe(
    desde="Fecha inicial inclusiva (AAAA-MM-DD)",
    hasta="Fecha final inclusiva (AAAA-MM-DD)",
)
async def horas(interaction: discord.Interaction, desde: str, hasta: str) -> None:
    if not interaction.guild:
        await interaction.response.send_message(
            "Este comando funciona dentro del servidor.", ephemeral=True
        )
        return
    if not HOURS_CHANNEL_IDS:
        await interaction.response.send_message(
            "Falta configurar HOURS_CHANNEL_IDS o LOG_CHANNEL_IDS en el bot.",
            ephemeral=True,
        )
        return
    user_roles = getattr(interaction.user, "roles", [])
    if REPORT_ROLE_IDS and not any(
        role.id in REPORT_ROLE_IDS for role in user_roles
    ):
        await interaction.response.send_message(
            "No tienes permiso para generar este informe.", ephemeral=True
        )
        return

    try:
        first = date.fromisoformat(desde)
        last = date.fromisoformat(hasta)
        if last < first:
            raise ValueError
    except ValueError:
        await interaction.response.send_message(
            "Usa fechas válidas en formato AAAA-MM-DD y asegúrate de que "
            "'hasta' no sea anterior a 'desde'.",
            ephemeral=True,
        )
        return

    start_utc = datetime.combine(first, time.min, tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    end_utc = datetime.combine(
        last + timedelta(days=1), time.min, tzinfo=LOCAL_TZ
    ).astimezone(timezone.utc)
    await interaction.response.defer(thinking=True, ephemeral=True)

    events: list[tuple[datetime, str, str, str]] = []
    scanned = 0
    skipped_channels: list[int] = []

    # Read a small buffer around the requested dates so overnight shifts can pair.
    history_after = start_utc - timedelta(days=2, seconds=1)
    history_before = end_utc + timedelta(days=2)
    for channel_id in HOURS_CHANNEL_IDS:
        channel = interaction.guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "history"):
            skipped_channels.append(channel_id)
            continue
        try:
            async for message in channel.history(
                limit=None,
                after=history_after,
                before=history_before,
                oldest_first=True,
            ):
                scanned += 1
                event = parse_service_event(message)
                if event is None:
                    continue
                kind, name, identifier, timestamp = event
                events.append((timestamp, kind, name, identifier))
        except (discord.Forbidden, discord.HTTPException):
            skipped_channels.append(channel_id)

    events.sort(key=lambda event: event[0])
    open_shifts: dict[str, tuple[str, datetime]] = {}
    total_seconds: dict[str, int] = defaultdict(int)
    shift_counts: dict[str, int] = defaultdict(int)
    pending_counts: dict[str, int] = defaultdict(int)
    canonical_names: dict[str, str] = {}

    for timestamp, kind, name, identifier in events:
        worker_key = name.casefold()
        canonical_names.setdefault(worker_key, name)
        if kind == "entrada":
            # Ignore duplicate entry events until the corresponding exit arrives.
            if identifier not in open_shifts:
                open_shifts[identifier] = (name, timestamp)
            continue

        opened = open_shifts.pop(identifier, None)
        if opened is None:
            continue
        start_name, start_time = opened
        local_start_date = start_time.astimezone(LOCAL_TZ).date()
        if first <= local_start_date <= last:
            worker_key = start_name.casefold()
            canonical_names.setdefault(worker_key, start_name)
            total_seconds[worker_key] += max(0, int((timestamp - start_time).total_seconds()))
            shift_counts[worker_key] += 1

    for name, start_time in open_shifts.values():
        local_start_date = start_time.astimezone(LOCAL_TZ).date()
        if first <= local_start_date <= last:
            worker_key = name.casefold()
            canonical_names.setdefault(worker_key, name)
            pending_counts[worker_key] += 1

    if not total_seconds and not pending_counts:
        await interaction.followup.send(
            f"No encontré turnos completos ni entradas pendientes entre "
            f"{first.isoformat()} y {last.isoformat()}. Revisé {scanned} mensajes.",
            ephemeral=True,
        )
        return

    worker_keys = sorted(
        set(total_seconds) | set(pending_counts),
        key=lambda key: (-total_seconds[key], canonical_names[key].casefold()),
    )

    def duration_text(seconds: int) -> str:
        minutes = seconds // 60
        return f"{minutes // 60}:{minutes % 60:02d}"

    def wage_for(seconds: int) -> int:
        amount = Decimal(seconds) * Decimal(15000) / Decimal(3600)
        return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    total_width = max(
        len("Pago"),
        *(len(format_pesos(wage_for(total_seconds[key]))) for key in worker_keys),
    )
    name_width = min(max(len("Trabajador"), *(len(canonical_names[key]) for key in worker_keys)), 30)
    shifts_width = max(len("Turnos"), *(len(str(shift_counts[key])) for key in worker_keys))
    pending_width = max(len("Pend."), *(len(str(pending_counts[key])) for key in worker_keys))
    hours_width = max(len("Horas"), *(len(duration_text(total_seconds[key])) for key in worker_keys))

    def hours_row(name: str, shifts: int, seconds: int, pending: int) -> str:
        shown_name = name if len(name) <= name_width else name[:name_width - 3] + "..."
        return (
            f"{shown_name:<{name_width}}  {shifts:>{shifts_width}}  "
            f"{duration_text(seconds):>{hours_width}}  "
            f"{format_pesos(wage_for(seconds)):>{total_width}}  "
            f"{pending:>{pending_width}}"
        )

    header = (
        f"{'Trabajador':<{name_width}}  {'Turnos':>{shifts_width}}  "
        f"{'Horas':>{hours_width}}  {'Pago':>{total_width}}  "
        f"{'Pend.':>{pending_width}}"
    )
    separator = "-" * len(header)
    data_rows = [
        hours_row(
            canonical_names[key],
            shift_counts[key],
            total_seconds[key],
            pending_counts[key],
        )
        for key in worker_keys
    ]
    heading = f"Horas · {first.isoformat()} a {last.isoformat()}"

    pages: list[list[str]] = []
    page: list[str] = []
    for row in data_rows:
        candidate = page + [row]
        if len("\n".join([heading, "```text", header, separator, *candidate, "```"])) > 1850 and page:
            pages.append(page)
            page = [row]
        else:
            page = candidate
    if page:
        pages.append(page)

    total_worked = sum(total_seconds.values())
    total_shifts = sum(shift_counts.values())
    total_pending = sum(pending_counts.values())
    for page_index, page_rows in enumerate(pages):
        footer: list[str] = []
        if page_index == len(pages) - 1:
            footer.extend([
                separator,
                hours_row("TOTAL", total_shifts, total_worked, total_pending),
            ])
            if skipped_channels:
                footer.append(
                    f"\nNo pude leer {len(skipped_channels)} canal(es); "
                    "verifica los permisos del bot."
                )
        embed = discord.Embed(
            title=f"⏱️ {heading}",
            description=f"```text\n{render_table(header, page_rows + footer)}\n```",
            color=discord.Color.from_rgb(52, 152, 219),
        )
        embed.set_footer(
            text=f"{LOCAL_TZ.key} · $15.000 por hora · Pendientes no incluidos en pago"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="top_semanal",
    description="Muestra los tres primeros en ventas de esta semana",
)
async def top_semanal(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message(
            "Este comando funciona dentro del servidor.", ephemeral=True
        )
        return
    if not LOG_CHANNEL_IDS:
        await interaction.response.send_message(
            "Falta configurar LOG_CHANNEL_IDS en el bot.", ephemeral=True
        )
        return
    user_roles = getattr(interaction.user, "roles", [])
    if REPORT_ROLE_IDS and not any(
        role.id in REPORT_ROLE_IDS for role in user_roles
    ):
        await interaction.response.send_message(
            "No tienes permiso para generar este informe.", ephemeral=True
        )
        return

    now_local = datetime.now(LOCAL_TZ)
    first = now_local.date() - timedelta(days=now_local.weekday())
    start_utc = datetime.combine(first, time.min, tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    now_utc = now_local.astimezone(timezone.utc)
    await interaction.response.defer(thinking=True, ephemeral=True)

    totals: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    canonical: dict[str, str] = {}
    scanned = 0
    skipped_channels: list[int] = []

    for channel_id in LOG_CHANNEL_IDS:
        channel = interaction.guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "history"):
            skipped_channels.append(channel_id)
            continue
        try:
            async for message in channel.history(
                limit=None,
                after=start_utc - timedelta(seconds=1),
                before=now_utc + timedelta(seconds=1),
                oldest_first=True,
            ):
                scanned += 1
                parsed = parse_sale(message)
                if parsed is None:
                    continue
                name, total = parsed
                key = name.casefold()
                canonical.setdefault(key, name)
                totals[key] += total
                counts[key] += 1
        except (discord.Forbidden, discord.HTTPException):
            skipped_channels.append(channel_id)

    if not totals:
        await interaction.followup.send(
            f"No encontré ventas desde el lunes {first.isoformat()}. "
            f"Revisé {scanned} mensajes.",
            ephemeral=True,
        )
        return

    ranking = sorted(
        totals,
        key=lambda key: (-totals[key], -counts[key], canonical[key].casefold()),
    )[:3]
    name_width = max(
        len("Trabajador"),
        *(min(len(canonical[key]), 30) for key in ranking),
    )
    position_width = len("Puesto")
    sales_width = max(len("Ventas"), *(len(str(counts[key])) for key in ranking))
    sold_width = max(
        len("Total vendido"),
        *(len(format_pesos(totals[key])) for key in ranking),
    )
    pay_width = max(
        len("30% a pagar"),
        *(len(format_pesos(totals[key] * 30 // 100)) for key in ranking),
    )

    def rank_row(position: str, key: str) -> str:
        name = canonical[key]
        shown_name = name if len(name) <= name_width else name[:name_width - 3] + "..."
        return (
            f"{position:>{position_width}}  {shown_name:<{name_width}}  "
            f"{counts[key]:>{sales_width}}  {format_pesos(totals[key]):>{sold_width}}  "
            f"{format_pesos(totals[key] * 30 // 100):>{pay_width}}"
        )

    header = (
        f"{'Puesto':>{position_width}}  {'Trabajador':<{name_width}}  "
        f"{'Ventas':>{sales_width}}  {'Total vendido':>{sold_width}}  "
        f"{'30% a pagar':>{pay_width}}"
    )
    separator = "-" * len(header)
    rank_symbols = ["①", "②", "③"]
    table_rows = [
        rank_row(rank_symbols[index - 1], key)
        for index, key in enumerate(ranking, 1)
    ]
    heading = f"Top 3 de ventas · {first.isoformat()} al {now_local.date().isoformat()}"
    footer = []
    if skipped_channels:
        footer.append(
            f"\nNo pude leer {len(skipped_channels)} canal(es); "
            "verifica los permisos del bot."
        )
    embed = discord.Embed(
        title=f"🏆 {heading}",
        description=f"```text\n{render_table(header, table_rows + [separator] + footer)}\n```",
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"Semana en curso · {LOCAL_TZ.key} · Top por total vendido")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.event
async def on_ready() -> None:
    print(f"Conectado como {bot.user} (ID: {bot.user.id})")


token = os.getenv("DISCORD_BOT_TOKEN")
if not token:
    raise RuntimeError("Configura DISCORD_BOT_TOKEN en las variables de entorno.")
bot.run(token)
