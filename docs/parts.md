# Parts inventory

Keep track of the hardware you have on hand (screws, heat-set inserts, nuts, washers, magnets, bearings, standoffs and so on) on the **Parts** page. When a design needs hardware, the design agent can design around what you have, or pick standard parts and give you a shopping list.

## Adding parts

- **Add from catalog** lists the parts in the catalogs you have turned on (see [Parts catalogs](#parts-catalogs)). Browse by category or type to search. You can also paste a part number or product link, or type a name, and select **Look up** (or press Enter) to find that part directly. Enter how many you have and select **Add**. Adding a part you already have adds to its quantity.
- **Custom part** is for anything not in a catalog. Give it a name, and optionally a category, size, links and notes. Custom parts can always be added, whichever catalogs are on.
- **Import CSV** merges a spreadsheet into your inventory (rows match by id, then catalog key, then name; others are added). **Export CSV** downloads your inventory in the same format, so the easiest way to make a CSV is to export one first.

Use **−** and **+**, or type a number, to change how many you have. Set **Warn when at or below** on a part to have it highlighted as **Low** when stock runs down.

Every catalog part carries the hole and pocket sizes the agent needs to model it: clearance and pilot holes, counterbores and countersinks for screws, hole size and depth for heat-set inserts, nut traps, magnet pockets and bearing seats. The agent uses these exact sizes, so parts fit.

## Parts catalogs

Choose where parts come from in **Settings > Parts and shopping > Parts catalogs**. **Add from catalog** and the design agent only use the catalogs that are on. Each part in your inventory remembers which catalog it came from and its part number or link there, and its buy links come from that catalog.

### Bambu's Maker Supply (on by default)

Over 100 common parts, built in: metric screws (M2 to M5, socket head, button head and countersunk), heat-set inserts, hex and nyloc nuts, washers, disc magnets, ball bearings, standoffs and a few motion parts. Each part has the hole and pocket sizes the agent needs (see below). Buy links go to the Bambu Lab Store for your region.

Besides searching, you can paste a Bambu Lab Store product link into **Add from catalog** and select **Look up**. Buildplate suggests the matching catalog parts (choose one to get its hole sizes), or you can add the linked product as it is, linked to that product page.

Turn it off if you don't buy from Bambu Lab. Parts you already added stay in your inventory.

### McMaster-Carr (optional)

McMaster-Carr parts are looked up live through McMaster-Carr's Product Information API. The API is only for approved McMaster-Carr customers. Request access from McMaster-Carr, and they send you an API username and password plus a client certificate (a `.pfx` file with its own password).

1. Open **Settings > Parts and shopping**. Under **McMaster-Carr**, enter the API username and password, choose the certificate file, and enter its password.
2. Select **Connect**. Buildplate logs in to McMaster-Carr to check the details before saving them, then turns McMaster-Carr on.

The login and certificate are stored encrypted and never sent to your browser (see [Security](security.md)). Use the switch to turn McMaster-Carr off without forgetting the login, or **Disconnect** to remove it.

McMaster-Carr's API has no search. To add a part, find it on mcmaster.com, then paste its part number (such as `91290A115`) or its page link into **Add from catalog** and select **Look up**. Parts you have looked up are saved, so after that they show up when you search, both in **Add from catalog** and for the design agent. Lookups are also kept for a week, because McMaster-Carr limits how many new parts an account can look up each day. When a McMaster-Carr part matches a standard part in Bambu's Maker Supply (for example an M3 x 8 socket head screw), the agent uses that part's hole sizes. Check them against McMaster-Carr's drawing for anything unusual.

## Hardware in Studio

The **Hardware** menu under the Studio prompt decides how a request handles hardware:

| Choice | What the agent does |
| --- | --- |
| **Ask me** | When your request clearly needs hardware (screws, inserts, magnets, bearings, hinges, wall mounting...), Buildplate asks which of the three below to use before spending any tokens. Otherwise it prefers parts you have. |
| **Use parts I have** | Uses only parts in your inventory, and no more than you have. A design that needs anything else is not saved until the agent changes it. |
| **Buy parts as needed** | Picks the best standard parts for the job, preferring ones you have, and lists what to buy. |
| **No hardware** | Uses printed alternatives: snap fits, printed threads, press-fit pins, living hinges. If hardware is truly unavoidable, the agent explains why. |

Follow-up requests on a design keep the choice you made for it. Set the default in **Settings > Parts and shopping**.

## The parts list

The **Parts** tab next to the viewer lists every part the current version uses, how many you have, and what to buy, with buy links from the catalog each part came from (McMaster-Carr or the Bambu Lab Store) plus Amazon. Library cards show how many parts a design uses and how many kinds you need to buy.

When you have printed and assembled a design, select **Mark as built** to take its parts out of your inventory. Buildplate remembers exactly what it took, so **Undo** puts them back. Inventory never changes on its own.

## Buy links

Buy links follow where a part came from. McMaster-Carr parts link to their McMaster-Carr product page. Bambu's Maker Supply parts link to the product page you added them from, or a search of the Bambu Lab Store for your region. Every part also gets a search on your Amazon site. Custom parts get a Bambu Lab Store search while Bambu's Maker Supply is on. Set the store region and Amazon site in **Settings > Parts and shopping**, along with an optional **Amazon associate tag** that is added to Amazon links if you have an Amazon Associates account. To always use a particular product or shop for a part, edit the part and paste your own link. It replaces the search.

## MCP

MCP clients can use `list_parts`, `add_part` and `update_part_quantity`. `generate_design` takes `hardware_mode` (`ask`, `inventory`, `order` or `none`) and returns the bill of materials; with `ask`, a request that needs hardware returns a question to put to the user. `get_design` includes the bill of materials. See [MCP integration](mcp.md).
