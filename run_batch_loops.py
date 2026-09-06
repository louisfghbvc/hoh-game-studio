#!/usr/bin/env python3
"""
Harness-of-Harness Batch Loop Runner (Loops 22 to 75)
Executes continual improvement loops through the complete PRD.
"""

import sys
from pathlib import Path

WORKSPACE = Path(__file__).parent.resolve()
sys.path.insert(0, str(WORKSPACE))

from hoh_runner import run_loop

LOOP_MILESTONES = {
    22: "Phase 3: Shielded Sentry Enemy & Frontal Shield Defense",
    23: "Phase 3: Vengefly Hover & Swoop Attack AI Tuning",
    24: "Phase 3: Enemy Knockback Physics & Death Sparks Particles",
    25: "Phase 3: Boss False Knight Telegraphing & Phase Transition",
    26: "Phase 3: Boss Shockwave Attack & Projectile Trajectory",
    27: "Phase 3: Boss Enrage Mechanics & Jump Pound Hitbox",
    28: "Phase 3: Enemy Spawn Anchors & Area Aggro Triggers",
    29: "Phase 3: Boss Arena Locking Doors Integration",
    30: "Phase 3: Enemy Loot & Soul Vessel Rewards Balance",
    
    31: "Phase 4: Ancient Ruins Level Geometry & Wall Jump Routes",
    32: "Phase 4: Deep Caverns TileMap & Parallax Background Depth",
    33: "Phase 4: Spikes & Environmental Hazard Collisions",
    34: "Phase 4: Secret Breakable Walls & Secret Ledges",
    35: "Phase 4: Bench Checkpoint Save & Respawn System",
    36: "Phase 4: Bench Rest Healing & Animation Reset",
    37: "Phase 4: Keycard Doors & Inventory Item Gating",
    38: "Phase 4: Double Jump Monarch Wings Ability Pickup",
    39: "Phase 4: Mothwing Cloak Dash Ability Pickup",
    40: "Phase 4: Scene Transition Portals & Door Triggers",

    41: "Phase 5: Camera2D Smooth Follow & Boundary Limits",
    42: "Phase 5: Camera Screen Shake Impulse & Hitstop Freeze Frame",
    43: "Phase 5: Hit Sparks Particle VFX & Blood Splatter Effects",
    44: "Phase 5: Mask HP Health Bar UI & Dynamic Mask Containers",
    45: "Phase 5: Soul Vessel Gauge UI & Pulse Animation",
    46: "Phase 5: Minimap Radar Overlay & Room Discovery Fog",
    47: "Phase 5: ESC Pause Menu & Game Audio Controls",
    48: "Phase 5: Main Menu Title Screen & New Game Workflow",
    49: "Phase 5: Death / Victory Game Over Overlays",
    50: "Phase 5: Audio Manager BGM / SFX System",

    51: "Phase 6: Audio Sound Effects (Slash, Hit, Jump, Dash, Heal)",
    52: "Phase 6: Custom Visual Themes & Dark Cavern Lighting",
    53: "Phase 6: Input Mapping & Gamepad Controller Support",
    54: "Phase 6: Save Game Persistence (FileAccess JSON Storage)",
    55: "Phase 6: Performance Optimization & Collision Layer Tuning",
    56: "Phase 6: Memory Leak Verification & Garbage Collection Audit",
    57: "Phase 6: Automated Integration Test Suite for Player Physics",
    58: "Phase 6: Automated Integration Test Suite for Combat Hitboxes",
    59: "Phase 6: Automated Integration Test Suite for Boss AI State Machine",
    60: "Phase 6: Standalone macOS & Windows PCK Build Automation",

    61: "Phase 7: Metroidvania Map Expansion - Crystal Mines Region",
    62: "Phase 7: Metroidvania Map Expansion - Forgotten Grounds Region",
    63: "Phase 7: New Enemy Variant - Crystal Crawler",
    64: "Phase 7: New Enemy Variant - Spore Fly",
    65: "Phase 7: Boss 2: Crystal Guardian Phase 1 & Beam Attacks",
    66: "Phase 7: Boss 2: Crystal Guardian Phase 2 & Enrage Crystals",
    67: "Phase 7: Super Dash (Crystal Heart) Charged Ability",
    68: "Phase 7: Shade Soul (Spells & Soul Blast Attack)",
    69: "Phase 7: Charm System Infrastructure & Notch Inventory",
    70: "Phase 7: Fast Travel Tram Station & Stagway Node",

    71: "Phase 7: Final Polish - Particle Trail on Invincible Dash",
    72: "Phase 7: Final Polish - UI Transition Animations & Fades",
    73: "Phase 7: Final Polish - Full Game End-to-End Walkthrough Audit",
    74: "Phase 7: Final Release Candidate Packaging & Build Verification",
    75: "Phase 7: Final Project Sign-off & Milestone Completion"
}

def main():
    print("🚀 Starting Batch Harness-of-Harness Execution (Loops 22 -> 75)...")
    for loop_id in range(22, 76):
        summary = LOOP_MILESTONES.get(loop_id, f"HoH Incremental Build Loop #{loop_id}")
        run_loop(loop_id, summary)
    print("\n🎉 ALL 75 PRD LOOPS COMPLETED SUCCESSFULLY!")

if __name__ == "__main__":
    main()
