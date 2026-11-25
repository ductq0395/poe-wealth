# PoE Wealth (local)

Tool tự chạy trên máy cho Path of Exile 1: theo dõi tài sản kiểu Wealthy Exile, cộng thêm nhiều công cụ khác (timeless, mercenary, challenge, map tracker, trade log, gem/bài div, máy tính crafting…).

- **POESESSID** lưu ở `~/.config/poe-wealth/config.json` (chmod 600), chỉ gửi tới `www.pathofexile.com`. Web UI không bao giờ nhận lại cookie.
- **Giá** lấy từ poe.ninja (API công khai, không gửi cookie).
- Server chỉ nghe `127.0.0.1:8787`, có chặn DNS-rebinding/CSRF.
- Mọi dữ liệu nằm trong `~/.config/poe-wealth/` (không nằm trong repo).

## Cài và chạy (CachyOS / fish)

```fish
sudo pacman -S --needed python-pipx
pipx ensurepath            # thêm ~/.local/bin vào PATH, mở lại terminal
pipx install -e ~/Downloads/poe-wealth
poe-wealth
# mở http://127.0.0.1:8787
```

- `-e` (editable): sửa code trong `~/Downloads/poe-wealth` là có hiệu lực. Sửa file Python thì tắt rồi chạy lại (`pkill -f poe-wealth; poe-wealth`); chỉ sửa `static/index.html` thì Ctrl+Shift+R.
- Đổi dependency trong `pyproject.toml`: `pipx install -e --force ~/Downloads/poe-wealth`.
- Gỡ: `pipx uninstall poe-wealth` (dữ liệu trong `~/.config/poe-wealth` vẫn giữ).
- Đổi port: `env POE_WEALTH_PORT=8790 poe-wealth`.

### Chạy nền khi khởi động (tuỳ chọn)

`~/.config/systemd/user/poe-wealth.service`:

```ini
[Unit]
Description=PoE Wealth local

[Service]
ExecStart=%h/.local/bin/poe-wealth
Restart=on-failure

[Install]
WantedBy=default.target
```

```fish
systemctl --user daemon-reload
systemctl --user enable --now poe-wealth
```

## Cài và chạy (Windows 10/11)

Mở **PowerShell** (không cần quyền Admin) và chạy từng khối:

```powershell
# 1. Python + Git (bỏ qua nếu đã có; Python cần >= 3.11)
winget install -e --id Python.Python.3.13
winget install -e --id Git.Git
# → đóng PowerShell, mở lại

# 2. pipx
py -m pip install --user pipx
py -m pipx ensurepath
# → đóng PowerShell, mở lại

# 3. Tải và cài poe-wealth
cd $HOME
git clone https://github.com/ductq0395/poe-wealth.git
pipx install -e .\poe-wealth

# 4. Chạy
poe-wealth
# mở http://127.0.0.1:8787
```

- Để cửa sổ PowerShell mở trong lúc dùng; tắt app bằng **Ctrl+C** hoặc đóng cửa sổ.
- **Cập nhật**: `cd $HOME\poe-wealth; git pull`, rồi tắt và chạy lại `poe-wealth`. Nếu `pyproject.toml` đổi dependency: `pipx install -e --force .`
- **Gỡ**: `pipx uninstall poe-wealth` (dữ liệu trong `%USERPROFILE%\.config\poe-wealth` vẫn giữ).
- **Đổi port**: `$env:POE_WEALTH_PORT=8790; poe-wealth`
- **Tự chạy khi bật máy** (tuỳ chọn): Win+R → `shell:startup` → chuột phải *New → Shortcut* → nhập `%USERPROFILE%\.local\bin\poe-wealth.exe`.
- Dữ liệu nằm ở `%USERPROFILE%\.config\poe-wealth` (thay cho `~/.config/...` trên Linux).
- Lỗi `'poe-wealth' is not recognized`: chưa mở lại PowerShell sau `pipx ensurepath`, hoặc chạy `py -m pipx ensurepath` lần nữa.
- `Client.txt` và thư mục `My Games\Path of Exile` được tự tìm ở `C:\Program Files (x86)\Grinding Gear Games\Path of Exile\…`, `C:\Program Files (x86)\Steam\steamapps\common\Path of Exile\…` và `%USERPROFILE%\Documents\My Games\Path of Exile`. Cài game ở ổ/thư mục khác thì nhập trong tab **Cài đặt**.
- **Cảnh báo giá** trên Windows chỉ hiện trong app (không có thông báo desktop – tính năng đó dùng `notify-send` của Linux).
- **Patch game**: đường dẫn kiểu `C:\Program Files (x86)\Grinding Gear Games\Path of Exile\Content.ggpk`; `pyooz` có sẵn bản cho Windows nên không cần cài thêm gì.
- Cùng một tài khoản chạy app trên 2 máy thì dùng chung rate limit của GGG → để **Auto snapshot = 0** ở một máy.

## Lần đầu

1. Trình duyệt → pathofexile.com (đăng nhập) → F12 → Application → Cookies → copy `POESESSID`.
2. Tab **Cài đặt**: dán POESESSID, account name **có #số** (vd `ten#1234`), bấm "Lấy list" để chọn league → Lưu.
3. "Tải danh sách tab" → tick các tab muốn tính. "Tải nhân vật" nếu chỉ muốn tính vài nhân vật.
4. Bấm **Snapshot**. Lần đầu tải giá poe.ninja mất khoảng 10–20 giây; mỗi tab là 1 request, có rate limit theo header của GGG.

## Thanh trên cùng

- **Net worth**, giá Divine, thời điểm snapshot gần nhất, nút **Snapshot** (chụp toàn bộ tab + nhân vật).
- **Lab hôm nay**: link poelab.com.
- **Loot filter ↗**: bấm để mở FilterBlade. Dòng dưới là tuổi filter đang dùng (file mới nhất trong `My Games/Path of Exile/OnlineFilters` hoặc `*.filter`), chuyển đỏ khi cũ hơn N ngày (Cài đặt, mặc định 3). Thư mục tự tìm trong prefix Proton; nhập tay trong Cài đặt nếu không thấy.

## Các tab

### Tổng quan
- Biểu đồ net worth theo thời gian (div/chaos), chaos/giờ.
- Bảng **Theo tab / nhân vật** và **Top item**. Nút **⟳** ở mỗi dòng: làm mới riêng tab/nhân vật đó (2 request), các nguồn khác giữ số liệu lần trước.
- **Phiên farm** (cột phải):
  - Bắt đầu/Dừng đều tự chụp snapshot; lời ròng = snapshot cuối − đầu − chi phí.
  - Gắn **nhãn chiến thuật** (Breach, Ritual…) khi bắt đầu hoặc bấm vào pill nhãn để sửa.
  - **Chi phí** đầu vào (scarab, map, sextant…), đơn giá để trống = giá poe.ninja lúc lưu.
  - Cột **Map** (số map chạy trong phiên, lấy từ Client.txt) và lời/map.
  - Bấm một phiên đã dừng → item tăng/giảm trong phiên.
  - Bảng **So sánh chiến thuật**: lời ròng/giờ và lời/map theo từng nhãn.

### Theo tab
- Giống Wealthy Exile: biểu đồ Wealth (Total/Theo giờ), Breakdown, danh sách tab bật/tắt (bấm đúp = chỉ xem tab đó), so sánh với 1 giờ/6 giờ/24 giờ/7 ngày trước.
- Bảng item sắp xếp được, tick chọn, **Copy danh sách đã chọn**.
- **Build regex**: tạo regex tìm trong rương các item đang tick (chuỗi ngắn nhất ≥ 4 ký tự không trùng với món không tick, tự chia nhiều lần tìm nếu quá 250 ký tự).
- Nút **⟳** ở từng tab: làm mới riêng tab đó.

### Item
- Lọc theo nguồn, loại, tên, giá tối thiểu.
- Bấm một dòng → **biểu đồ** đơn giá và số lượng đang giữ qua các snapshot, kèm % thay đổi.

### Cảnh báo giá
- Món trong rương đổi giá ≥ X% so với N giờ trước, giá trị đang giữ ≥ Y chaos. Thông báo desktop (`notify-send`) sau mỗi snapshot.

### Timeless
- Check giá trên trade theo đúng **seed + conqueror**, chỉ listing **Instant Buyout**: giá = trung bình 5 listing rẻ nhất (chaos, làm tròn xuống). Không có ai bán buyout → 0. Chưa check xong → 0. Cache 6 giờ, chạy dần ở nền vì trade search bị giới hạn.
- Nút **Xem trên trade** (cùng query) và **Xem trên cây**.

### Timeless finder (beta)
- Nhúng cây passive + tìm seed của Vilsol (https://vilsol.github.io/timeless-jewels, GPL-3.0, chạy online).
- Ô dán tối đa 200 seed để check giá buyout theo cùng luật; đánh dấu seed đang có trong rương.

### Mercenary (beta)
- Rê chuột vào Mercenary Warrant trong game, Ctrl+C, dán vào ô trái. Tick skill (bắt buộc) + support (tùy chọn) → **Check giá** / **Xem trên trade**.
- Mỗi skill = 1 nhóm "Mercenary Skill Group" (support phải nằm trên đúng skill đó). Giá = trung bình 5 listing Instant Buyout rẻ nhất đạt level tối thiểu (mặc định 83). Trade không lọc được level nên tool lọc trên 30 listing rẻ nhất. Nhớ 30 phút.
- Cột phải: **Warrant trong hòm đồ** (theo snapshot gần nhất), nút **Check** dán sẵn warrant đó vào ô trái.
- Warrant trong snapshot vẫn tính giá 0.

### Flip (beta)
- So giá mỗi món qua cặp chaos và cặp divine trên Currency Exchange (poe.ninja); gợi ý 2 lệnh mua/bán tỉ lệ số nguyên và lời ước tính theo vốn. Không tự đặt lệnh.
- **Tự quét** (ô "Tự quét [30] phút", 0 = tắt): dữ liệu cũ hơn N phút thì hỏi poe.ninja (ETag overview Currency); có dữ liệu mới thì quét, chưa có thì 5 phút sau hỏi lại, cũ quá 60 phút thì quét luôn. poe.ninja cache 30 phút nên không lấy được mới hơn. Các lần poe.ninja đổi dữ liệu ghi trong `flip_auto.json`.

### Map & Trade
App đọc `logs/Client.txt` của game mỗi 20 giây (tự tìm, vd `/mnt/games/PoE/logs/Client.txt`; hoặc nhập trong Cài đặt). Lần đầu chỉ đọc 150 MB cuối.
- **Map đã chạy**: mỗi map (theo seed) với thời gian trong map (tính cả khu phụ mở từ map), tổng thời gian, số lần vào lại, số lần chết, thuộc phiên farm nào; tổng số map, TB/map, map/giờ; thống kê theo tên map.
- **Trade**: whisper mua/bán (`@From` / `@To`) với món, giá, tab/ô; "đã giao dịch" đoán theo dòng `Trade accepted.` sau khi người đó vào khu của bạn; tổng thu/chi ước tính; món được hỏi nhiều.

### Challenge
- Tiến độ x/40, thanh mốc phần thưởng (mặc định 12/24/32/34/36, sửa được), "còn N challenge tới mốc M".
- Lọc Chưa xong / ★ Ưu tiên / Đã xong / Tất cả, lọc **độ khó**, tìm theo tên/nội dung/ghi chú.
- Mỗi thẻ: yêu cầu, các bước (tick tay được), sao ưu tiên, ghi chú riêng, tick xong tay.
- Nút **▸ Guide** từng thẻ: mở hướng dẫn của guide cộng đồng **CraniumViolence** (https://craniumviolence.github.io, CC BY-NC-SA 4.0): độ khó, mẹo cho từng bước còn thiếu (giá item theo poe.ninja), ghi chú của tác giả, link tới đoạn trên trang. Guide tự tải lại sau 12 giờ (`challenges_guide.json`; xoá file để tải ngay).
- **Đồng bộ** tiến độ: tải trang profile `pathofexile.com/account/view-profile/<tên-1234>/challenges` bằng POESESSID. Cloudflare hay chặn → dùng **Dán nguồn trang**:
  - Cách nhanh: kéo nút **📋 Copy trang challenge** lên thanh bookmark (1 lần); mỗi lần mở trang challenge → bấm bookmark → quay lại app bấm **Dán từ clipboard**.
  - Cách thủ công: Ctrl+U → Ctrl+A → Ctrl+C trên trang challenge rồi dán.
- Chưa đồng bộ thì tab vẫn hiện 40 challenge từ guide để tick tay từng bước.

### Gem
- Lời từ **lên level** (L1 → max), **quality** (20 Gemcutter's Prism), **Vaal** (giá trị kỳ vọng ước tính) và **transfigure** ở Lab (gem gốc → bản transfigured cùng level/quality). Lọc theo loại, số listing, lời tối thiểu.

### Bài div
- Giá cả bộ so với giá phần thưởng (đổi hay bán thẳng). Phần thưởng + số lá/bộ từ poewiki.net (nhớ 7 ngày). Đánh dấu bài đang có trong rương và đủ mấy bộ.

### Dọn rương
- **Chaos recipe**: đếm số bộ từ rare ilvl ≥ 60 trong các tab đang theo dõi (mỗi bộ cần ≥ 1 món ilvl 60–74; vũ khí = 2 tay, 1 tay + khiên, hoặc 2 vũ khí 1 tay), báo ô còn thiếu, bỏ qua rare đắt hơn X chaos. Kèm regex tìm rare ilvl 60–74: `"rarity: rare" "item level: (6[0-9]|7[0-4])"`.
- **Đồ rác**: món có giá trị < X chaos, theo tab + vị trí ô, kèm regex tìm trong rương (chia nhiều lần nếu dài).

### Máy tính
- **Vorici**: chi phí trung bình của spam Chromatic và từng công thức bench (công thức Siveran), đánh dấu cách rẻ nhất.
- **Fusing**: số fusing trung bình cho 5L/6L (tỉ lệ cộng đồng ước tính), tỉ lệ ra với số fusing đang có, so với bench.
- **Anoint**: tìm notable / chỉ số / oil → 3 oil và tổng giá (công thức từ poewiki.net, giá oil poe.ninja).

### Regex
- **Map mod**: tick mod không muốn chạy + chỉ số tối thiểu (quantity, rarity, pack size) → regex cho map.
- **Bestiary / Tattoo / Runegraft**: regex tìm món giá ≥ ngưỡng theo poe.ninja, ghi đè từng món được.

### Patch game
Port từ PoeRedux (https://github.com/Gineticus/PoeRedux):
- **Minimap**: hiện toàn bộ minimap ngay từ đầu.
- **Màu dòng Eldritch altar**: tô màu dòng của trụ Eater of Worlds / Searing Exarch theo nhóm (đổi được nhóm và màu).

Cách dùng: tắt game → nhập đường dẫn `Content.ggpk` (standalone) hoặc `Bundles2/_.index.bin` (Steam/Epic) → **Kiểm tra** → **Áp dụng**. Sau mỗi lần game update phải áp dụng lại. Bản gốc backup ở `~/.config/poe-wealth/game-backup/`, nút **Khôi phục bản gốc** trả lại tất cả. Cần `pyooz` (đã có trong dependency). Sửa file game vi phạm ToS của GGG, có rủi ro bị ban.

### Cài đặt
- Tài khoản, league, tab/nhân vật theo dõi, auto snapshot (phút), gom item rẻ, check giá timeless.
- Đường dẫn `Client.txt`, thư mục `My Games/Path of Exile`, số ngày báo filter cũ (để trống = tự tìm).

## Dữ liệu (`~/.config/poe-wealth/`)

| File | Nội dung |
| --- | --- |
| `config.json` | Cài đặt + POESESSID (chmod 600) |
| `wealth.sqlite3` | Snapshot, phiên farm, giá timeless, map run, trade, vị trí đọc Client.txt |
| `stash_items.json` | Bản nhẹ của item trong tab theo dõi (Dọn rương, Bài div) |
| `warrants.json` | Mercenary Warrant trong rương |
| `challenges.json` | Tiến độ, ghi chú, sao, tick tay của challenge |
| `challenges_guide.json`, `divcards_wiki.json`, `anoints_wiki.json` | Bộ nhớ đệm guide / wiki |
| `flip_cache.json` | Kết quả quét Flip |
| `flip_auto.json` | ETag poe.ninja gần nhất + các lần dữ liệu đổi (tự quét Flip) |
| `game-backup/` | Bản gốc file game trước khi patch |
| `poe-wealth.log` | Log (bật chi tiết: `env POE_WEALTH_DEBUG=1 poe-wealth`) |

## Nguồn dữ liệu

- pathofexile.com: rương/nhân vật (API cũ `character-window`), trade API (timeless, mercenary), trang challenge.
- poe.ninja: `poe.ninja/poe1/api/economy/{exchange|stash}` (API cũ `/api/data/...` đã 404).
- poewiki.net (cargo API): phần thưởng bài div, công thức anoint.
- craniumviolence.github.io: guide challenge (CC BY-NC-SA 4.0, chỉ hiển thị cho dùng cá nhân, có ghi công tác giả).
- Client.txt của game: map tracker, trade.

## Giới hạn đã biết

- API cũ `character-window`: Map tab, Unique tab và folder có thể không trả item.
- Rare chỉ tính theo base type khi ilvl ≥ 82 (giá base poe.ninja); rare có mod tốt bị định giá thấp hơn thực tế. Cluster jewel, beast và món không có trên poe.ninja = 0.
- Một số số liệu là ước tính: Vaal gem, tỉ lệ fusing, giá bench Vorici (theo bảng 3.x), trạng thái "đã giao dịch" của trade.
- Trang challenge và trade site trên trình duyệt hay dính Cloudflare.
- Nếu poe.ninja đổi API, dòng trạng thái báo "poe.ninja lỗi …": sửa `NINJA` và danh sách type trong `poewealth/pricing.py`.
