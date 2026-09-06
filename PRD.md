# 🗡️ 2D 類空洞騎士 (Metroidvania) 遊戲 - 產品需求文件 (PRD)

> **專案名稱**：VoidKnight (空洞騎士風格 2D 動作冒險)
> **遊戲類型**：2D Metroidvania / 類銀河戰士惡魔城
> **遊戲引擎**：Godot 4 (GDScript, CharacterBody2D, TileMap, AnimationPlayer)

---

## 🎯 一、 核心玩法與遊戲機制 (Core Loop)
- **物理與動作**：高反應度的 2D 平台跳躍（包含 Coyote Time 殘影時間、Jump Buffering 預按跳躍、牆壁滑行 Wall Slide 與壁跳 Wall Jump）。
- **衝刺機制 (Dash)**：短距離無敵衝刺 (Invincible Dash)，冷卻時間 0.6 秒。
- **近戰擊退與下斬 (Pogo Attack)**：
  - 四方向揮劍（左、右、上、下）。
  - 空中下斬擊中敵人或棘刺可產生「下彈 (Pogo Jump)」跳躍。
- **靈魂集中與自癒 (Soul & Focus Healing)**：
  - 攻擊敵人群聚獲得靈魂 (Soul Vessel)。
  - 長按技能鍵消耗靈魂恢復 1 點血量。

---

## 🗺️ 二、 地圖與關卡設計 (Level Design)
- **無縫連通地圖**：採用 Godot 4 `TileMap` 設計古老遺跡 (Ancient Ruins) 與深淵洞窟。
- **存檔點 (Bench Checkpoint)**：玩家可在長椅休息，恢復全血量，並更新復活點。
- **能力解鎖過關 (Ability Gating)**：取得「二段跳」或「衝刺」能力後方可進入新區域。

---

## 👾 三、 敵人與 Boss 戰 (Enemy AI)
1. **爬行蟲 (Crawler)**：沿著地面/牆壁巡邏。
2. **飛行毒蜂 (Vengefly)**：在空中懸停，發現玩家後俯衝攻擊。
3. **盾牌騎士 (Shielded Sentry)**：正面防禦，需要透過衝刺繞後攻擊。
4. **守衛 Boss (False Knight)**：具備前搖預告 (Telegraphing) 的重擊、震波與跳躍砸地攻擊。

---

## 🎨 五、 打磨與打擊感 (Juice & VFX)
- **畫面震撼感**：擊中頓幀 (Hitstop Freeze Frame) 與 Camera2D 震動效果。
- **粒子特效**：砍擊火花、靈魂粒子吸取、受擊血滴特效。
- **UI & HUD**：面具血量條 (Mask Health)、靈魂容器 (Soul Vessel)。

---

## 🚀 六、 HoH 50~70 輪迭代里程碑藍圖 (Milestones Roadmap)

### Phase 1: 基礎動作與物理 (Loops 1~15)
- [ ] Loop 01: 建立 Godot 2D 專案基礎目錄結構與 main 2D 場景。
- [ ] Loop 02: 實作 CharacterBody2D 基礎移動 (Speed, Gravity, Jump)。
- [ ] Loop 03: 加入 Coyote Time 與 Jump Buffer 提升跳躍手感。
- [ ] Loop 04: 實作 Wall Slide (攀牆) 與 Wall Jump (壁跳)。
- [ ] Loop 05: 實作 Dash (衝刺) 與冷卻計時器。

### Phase 2: 戰鬥系統與攻擊 Hitbox (Loops 16~30)
- [ ] Loop 16: 建立 Area2D Hitbox / Hurtbox 攻擊判定架構。
- [ ] Loop 17: 實作四方向揮劍攻擊 (左右上) 動畫與判定。
- [ ] Loop 18: 實作空中下斬 (Down Attack) 與 Pogo 跳躍彈回。
- [ ] Loop 19: 實作靈魂值 (Soul) 累積與長按集中治療 (Focus Heal)。
- [ ] Loop 20: 實作受擊無敵時間 (Invincibility Frames) 與閃爍特效。

### Phase 3: 敵人 AI 與 Boss 戰 (Loops 31~45)
- [ ] Loop 31: 實作基礎爬行敵人的巡邏與碰撞傷害。
- [ ] Loop 32: 實作飛行敵人 (Vengefly) 的追逐與攻擊 AI。
- [ ] Loop 33: 實作敵人的受擊擊退 (Knockback) 與死亡粒子。
- [ ] Loop 34: 實作 Boss 的階段性攻擊狀態機 (Charge, Ground Pound, Idle)。

### Phase 4: 地圖、長椅存檔與能力解鎖 (Loops 46~60)
- [ ] Loop 46: 繪製 TileMap 關卡（包含地面、棘刺 traps、隱藏牆壁）。
- [ ] Loop 47: 實作長椅 (Bench) 存檔點與血量恢復。
- [ ] Loop 48: 實作能力道具（取得「二段跳」雙重跳躍能力）。

### Phase 5: 打擊感打磨與 UI/音效 (Loops 61~75+)
- [ ] Loop 61: 實作 Camera2D 平滑跟隨、邊界限制與 Screen Shake 震動。
- [ ] Loop 62: 加入 Hitstop 擊中頓幀效果。
- [ ] Loop 63: 完成面具血條 UI 與靈魂容器視覺顯示。
- [ ] Loop 64: 加入遊戲主選單與 Death/Game Over 畫面。
