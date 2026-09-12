# CLAUDE.md — 防災・交通情報モニタ

水先人会の事務所に常設するモニタ用ボード。スクロール・クリックなしの一画面表示。
利用者向けの説明は README.md、ここは開発を引き継ぐための技術メモ。

## 構成
- `bousai_board.py`：Python 3.9+、標準ライブラリのみ（Windows PCで常駐させる前提。外部ライブラリを増やさない）
  - `src_*()` 関数がデータ源ごとに1つ。スレッドで `intervals` ごとに実行し、結果を `STATE` に保持
  - `build_status()` が `/api/status` のJSONを組み立てる。発令条件の判定は `build_alerts()`
  - 取得失敗時は前回値を残し `stale` を立てる。画面は「データ取得遅延」を出す
  - JMAの一覧系は ETag/Last-Modified で差分取得（`http_get(conditional=True)`）。詳細電文は `get_detail()` でキャッシュ
- `bousai_board.html`：15秒ごとに `/api/status` を取得して描画。`fit()` がはみ出しを検知して文字縮小→詳細を1行化→「ほか n 件」
- `?demo=1`：本物のデータに警報・運休のダミーを重ねて表示（レイアウト確認用）
- `config.json`：利用者が触る設定。既定値は `DEFAULT_CONFIG`

## 発令条件（依頼者の指定。勝手に変えない）
1. 愛知県・三重県北中部が対象
   - 最大震度5弱以上、または長周期地震動階級3以上
   - 予想される津波の最大波が3m超（＝大津波警報）
   - レベル5特別警報（河川氾濫・大雨・土砂災害・高潮）
   - 特別警報（気象）：暴風・波浪・暴風雪・大雪
2. 全国で震度6弱以上

## データ源と注意点
| 対象 | URL | メモ |
|---|---|---|
| 警報 | `jma.go.jp/bosai/warning/data/r8/{230000,240000}.json` | **2026-05-29 の新防災気象情報で形式変更**。旧 `warning/data/warning/*.json` は5/28で更新停止。配列（VPWW55〜61の複数電文）。class10Items/class20Items の kinds[].code と status（"解除"は除外） |
| 警報コード | — | Lv5：33大雨 39土砂 38高潮／Lv4：43 49 48／気象特別警報：35暴風 32暴風雪 36大雪 37波浪。気象庁サイトJS内の対応表から取得 |
| 河川氾濫 | `bosai/flood/data/r8/flood_xml.json` | item.code 51/53=レベル5氾濫特別警報、40/41=レベル4。class10Codes で地域判定。**河川名のキーは未確認**（平常時は空配列で実物を見られていない） |
| 地域 | `bosai/common/const/area.json` | class20→class15→class10。対象 class10：230010 230020 240010 |
| 地震 | `bosai/quake/data/list.json` ＋詳細JSON | maxi は "5-" 形式。1地震（eid）に 震度速報→震源に関する情報→震源・震度情報 と複数電文が入るので、`_final_reports()` で震度を持つ最新の電文を確定値にする（下方修正も反映。取消電文があれば地震ごと除外）。三重は詳細電文の Area.Code で判定（450愛知東部 451愛知西部 460三重北部 461三重中部）。**461は他コードからの推定、実データ未確認**。詳細電文が取れないときは県単位（list.json の int[].code 23/24）で判定するため、三重県南部だけの震度5弱以上も「三重県（区域は確認中）」として発令表示になる（安全側の意図的な動作） |
| 長周期 | `bosai/ltpgm/data/list.json` | lg[].code / maxLg |
| 津波 | `bosai/tsunami/data/list.json` ＋詳細 | 最新の VTSE41。Body.Tsunami.Forecast.Item[].Area.Name / Category.Kind.Name / MaxHeight.TsunamiHeight。対象：伊勢・三河湾、愛知県外海 |
| JR東海 | `traininfo.jr-central.co.jp/zairaisen/data/trainInfo/json/unkou.json` | 平常時 events=null。events[].imp_line/status/cause、message_info[i].delivery_msg。路線マスタ `hp_senku_master_ja.json` |
| 地下鉄 | `kotsu.city.nagoya.jp/datas/latest_traffic.json` | rosen_id（M_LINE＝名城・名港）、icon_cd N/W/C/E |
| 名鉄 | `top.meitetsu.co.jp/em/`（HTML） | JSONなし。平常は `p.emLv00`「15分以上の列車の遅れはございません。」。異常時は `div.emInfo.emLvNN` の中に h2＝状態、`ul.emListLine>li`＝対象線区、表の「路線／理由／備考」。レベルは CSS の定義で 01 運転見合せ／02 遅延・一部運休／03 振替輸送／04 バス代行輸送／05 その他（2026-09-13 `em/css/basic.css` で確認）。**停止判定はレベル番号 01 で行う**（名鉄は「見合せ」と送り仮名なしで書くため文言判定は保険扱い）。線区判定は emListLine を使う（文章マッチは誤検知するので戻さない）。どちらも無ければ構造変更とみなして例外。提供時間外（0:31〜4:59）の文言差し替えは**ページ内JSが `#descriptionText` を書き換えているだけ**で取得HTMLには出ないため、JSと同じ条件を `off_hours` で判定する（2026-09-13 実物で確認） |
| あおなみ線 | `aonamiline.co.jp/railinfo`（HTML） | `table.delay_table` の td |
| 伊勢湾岸道 | `ihighway.jp/datas/json/traffic.json` | NEXCO中日本。trafficInfo/otherTrafficInfo のカテゴリ別。区間判定は `icInfoApp.json` の pointX（東海JCT 5489〜みえ川越 5278）と、イベントの coordinate または題名のIC名。フィールドは jam が `distance`(km)・`passing.section`/`passing.time`・`title`(渋滞先頭)、規制系が `detail`＋`reason`、その他は `title`＋`reason` |
| 国道23号 愛知側（保留） | `cbr.mlit.go.jp/meikoku/cms/{kisei,kinkyu}/?view=index` | 名古屋国道事務所。事故・渋滞は含まない。JARTIC は規約面で採用せず |
| 国道23号 三重側（未実装） | `cbr.mlit.go.jp/mie/kouji/` ／ `mie/emergency_lists.php` ／ カメラ `mie/live/touge/cam_data/Jpeg058.jpg` | **三重河川国道事務所**（北勢国道事務所ではない。北勢は1号・25号・475号のみ）。工事規制予定は毎日20時に翌日分、HTML表の「愛知県境〜四日市市中里」が みえ川越IC〜四日市千歳町（380〜393KP）。カメラは 392.5KP 四日市市海山道「四日市高架橋」（区間内で唯一、約15分更新）。事故・渋滞は無し |

## 開発時の確認方法
- 各 `src_*()` を単体で呼んで実データを確認できる（`python -c "import bousai_board as b; print(b.src_jr())"`）
- 警報判定は `get_json` を差し替えてダミーデータで確認した（地震・津波・Lv5/Lv4・河川・気象特別警報）
- 1920×1080 で `?demo=1` を表示し、はみ出しがないこと

## 未対応・要確認
- [x] 名鉄の異常時HTML構造を確認し、線区判定を `ul.emListLine` ベースに変更（2026-09-11 の遅延時に実物を確認）
- [ ] 河川氾濫の河川名キー（`flood_xml.json` は平常時 `[]`。発生時にしか確認できない）
- [ ] 三重県中部 461：実データで 450 愛知県東部・451 愛知県西部・460 三重県北部・462 三重県南部を確認。並びから 461＝三重県中部はほぼ確実だが、461そのものの観測はまだ
- [ ] 国道23号（保留中。何を出すか未決）。情報源は 2026-09-11 に調査済みで上表のとおり。事故・渋滞のリアルタイムは Google地図の交通状況（`google_maps_api_key`、実装済み）以外に手段なし。国交省 道路情報提供システム `road-info-prvs.mlit.go.jp`（5〜10分更新）はデータが毎回変わる難読パスの下で壊れやすく不採用。三重県道路規制情報は県管理道路のみで23号は対象外
- [x] ログのファイル出力（`board.log`。1MB×3世代でローテーション。2026-09-12）
- [ ] PC起動時の自動実行の手順確認（`start_board.bat` はサーバーが応答するまで待つようにした。タスクスケジューラ／スタートアップへの登録手順は未確認）
