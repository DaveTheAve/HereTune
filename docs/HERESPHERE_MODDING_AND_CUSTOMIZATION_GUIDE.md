# HereSphere Modding & Customization Guide

> **Unofficial community reference**  
> **Focus:** HereSphere customization/modification outside the Web API  
> **Last updated:** 2026-08-16  
> **Primary validation target:** Windows/Steam build with `HereSphere-Win64-Shipping.exe` dated 2025-04-30, supplemented by the 2024 HSP layout, 2,832 parsed saved-media presets, native HSP exports, and public HereSphere release notes through v0.12.0. The public download page also lists newer 0.12.x builds; do not infer undocumented serialization changes from a version number alone.

This document describes the practical ways HereSphere can be customized, extended, inspected, backed up, and—where appropriate—modified **without relying on the HereSphere Web API**.

It is intentionally broader than an HSP specification and intentionally narrower than a general Unreal Engine reverse-engineering manual. The goal is to answer questions like:

- What can I customize without touching the executable?
- What exactly is stored in HSP profiles?
- How do filename suffixes affect projection/stereo detection?
- Where do SteamVR controller bindings live?
- Which saved-data files are worth backing up?
- What can realistically be changed in the Unreal `.pak`?
- What does the embedded Chromium/BLUI installation do?
- Which modifications are robust, and which will break every update?
- How do I inspect HereSphere safely without distributing proprietary game files?

This is **not official HereSphere LLC documentation**. The guide distinguishes between documented/supported behavior, observed current-build behavior, and experimental reverse-engineering territory.

For Web API development, use the separate:

- `HERESPHERE_API_QUICK_REFERENCE.md`
- `HERESPHERE_API_REFERENCE.md`

---

# Table of contents

1. [Modification levels at a glance](#1-modification-levels-at-a-glance)
2. [What the Windows installation contains](#2-what-the-windows-installation-contains)
3. [Back up before modifying anything](#3-back-up-before-modifying-anything)
4. [Filename-driven projection and stereo detection](#4-filename-driven-projection-and-stereo-detection)
5. [Image presets and global/default image behavior](#5-image-presets-and-globaldefault-image-behavior)
6. [Per-video HSP profiles](#6-per-video-hsp-profiles)
7. [What an HSP v7 profile can modify](#7-what-an-hsp-v7-profile-can-modify)
8. [Image correction fields in HSP v7](#8-image-correction-fields-in-hsp-v7)
9. [Projection, stereo and lens calibration through HSP](#9-projection-stereo-and-lens-calibration-through-hsp)
10. [Stitching, alignment, orientation and origin](#10-stitching-alignment-orientation-and-origin)
11. [Autofocus, motion and synchronization keyframes](#11-autofocus-motion-and-synchronization-keyframes)
12. [Audio, transitions and environment keyframes](#12-audio-transitions-and-environment-keyframes)
13. [Tags, ratings, favorites and resume metadata](#13-tags-ratings-favorites-and-resume-metadata)
14. [Editing HSP files safely](#14-editing-hsp-files-safely)
15. [External alpha videos and passthrough customization](#15-external-alpha-videos-and-passthrough-customization)
16. [Scripts and synchronized peripherals](#16-scripts-and-synchronized-peripherals)
17. [Subtitles and subtitle-related customization](#17-subtitles-and-subtitle-related-customization)
18. [Audio tracks and audio timing](#18-audio-tracks-and-audio-timing)
19. [Keybindings inside HereSphere](#19-keybindings-inside-heresphere)
20. [SteamVR binding files](#20-steamvr-binding-files)
21. [Saved data: library, settings, backups and keybindings](#21-saved-data-library-settings-backups-and-keybindings)
22. [The Unreal `.pak` package](#22-the-unreal-pak-package)
23. [What PAK modding can and cannot realistically do](#23-what-pak-modding-can-and-cannot-realistically-do)
24. [Embedded Chromium / BLUI](#24-embedded-chromium--blui)
25. [Native DLLs and the shipping executable](#25-native-dlls-and-the-shipping-executable)
26. [Debug logs and troubleshooting](#26-debug-logs-and-troubleshooting)
27. [A safe PAK inspection workflow](#27-a-safe-pak-inspection-workflow)
28. [A safe binary-inspection workflow](#28-a-safe-binary-inspection-workflow)
29. [Steam updates and keeping mods maintainable](#29-steam-updates-and-keeping-mods-maintainable)
30. [Useful modification recipes](#30-useful-modification-recipes)
31. [Things that look moddable but usually should not be modified](#31-things-that-look-moddable-but-usually-should-not-be-modified)
32. [Current-build observations and version caveats](#32-current-build-observations-and-version-caveats)
33. [Recommended community-tool architecture](#33-recommended-community-tool-architecture)
34. [Source notes and public references](#34-source-notes-and-public-references)

---

# 1. Modification levels at a glance

Not all “mods” are equal. HereSphere has several layers with very different stability and risk.

| Level | Examples | Stability | Recommended? |
|---|---|---:|---|
| **Supported player customization** | filename suffixes, image presets, keybindings, subtitles, scripts, external alpha, user settings | High | **Yes** |
| **HSP/profile tooling** | image corrections, projection fixes, lens/FOV calibration, tags, keyframes, audio offsets | High when version-aware | **Yes** |
| **Saved-data tooling** | backups, library inspection, keybinding migration | Medium | Yes, with backups |
| **Loose configuration/binding files** | SteamVR binding JSON | Medium | Yes, preferably through SteamVR UI |
| **Cooked PAK asset modding** | inspecting/replacing cooked assets, UMG/material experiments | Low–medium | Experimental |
| **CEF/BLUI replacement** | Chromium resources/binaries | Low | Usually no |
| **Native executable/DLL patching** | code hooks, binary edits, DLL replacement | Very low | Only for research/experiments |

A good rule is:

> **Prefer metadata/profile/input modifications over code modifications.**

HereSphere already exposes a surprisingly large amount of behavior through HSP profiles, presets, filename conventions, keybindings and companion files. Native patching should be a last resort, not the starting point.

---

# 2. What the Windows installation contains

A typical inspected Steam installation contains these important areas:

```text
HereSphere/
├── HereSphere.exe
├── Manifest_NonUFSFiles_Win64.txt
├── Engine/
│   └── ... Unreal Engine runtime libraries
└── HereSphere/
    ├── Binaries/Win64/
    │   ├── HereSphere-Win64-Shipping.exe
    │   ├── debug.log
    │   ├── libass-9.dll
    │   ├── libfreetype-6.dll
    │   ├── libfribidi-0.dll
    │   ├── libharfbuzz-0.dll
    │   └── libiconv-2.dll
    ├── Config/SteamVRBindings/
    │   ├── steamvr_manifest.json
    │   ├── knuckles.json
    │   ├── oculus_touch.json
    │   ├── vive_controller.json
    │   ├── vive_cosmos_controller.json
    │   ├── holographic_controller.json
    │   ├── gamepad.json
    │   └── ...
    ├── Content/Paks/
    │   └── HereSphere-WindowsNoEditor.pak
    └── Plugins/BLUI/ThirdParty/cef/Win/shipping/
        ├── libcef.dll
        ├── chrome_elf.dll
        ├── cef.pak
        ├── cef_extensions.pak
        ├── locales/
        └── ...
```

The inspected install is an Unreal Engine 4 application. The main application logic is compiled into `HereSphere-Win64-Shipping.exe`; cooked game content is primarily packaged in `HereSphere-WindowsNoEditor.pak`; SteamVR controller defaults are loose JSON files; and the in-app browser uses the BLUI plugin with Chromium Embedded Framework (CEF).

This separation is useful because it tells you **where not to look**:

- Want different projection metadata? Use HSP/filename conventions, not the PAK.
- Want a different controller mapping? Use HereSphere/SteamVR keybindings, not the EXE.
- Want to inspect a widget/material/asset? The PAK is the relevant layer.
- Want browser-engine behavior? CEF is involved.
- Want to change native player logic? That is shipping-binary territory and is substantially harder.

---

# 3. Back up before modifying anything

Before experimenting, preserve both **installation files** and **user data**.

## 3.1 Steam installation backup

At minimum, preserve:

```text
HereSphere-Win64-Shipping.exe
HereSphere-WindowsNoEditor.pak
HereSphere/Config/SteamVRBindings/
Manifest_NonUFSFiles_Win64.txt
```

Steam can restore installation files with **Verify integrity of game files**, but keeping the original files next to your experiment is faster and makes byte-level comparison possible.

## 3.2 User-data backup

The inspected saved-data archive contained files named:

```text
Default/Backup0.hsb
Default/Backup1.hsb
Default/Backup5.hsb
Default/KeyBindings.hsk
Default/Library.hsl
```

Treat these as binary application data, not hand-editable config files.

Before writing tools that modify them:

1. Exit HereSphere completely.
2. Copy the entire saved-data directory.
3. Keep several dated generations.
4. Modify a copy first.
5. Launch the player and confirm the library/settings load correctly.
6. Never assume a newer build will accept a file rewritten by an older parser.

## 3.3 HSP backup

If modifying `.hsp` profiles in bulk, keep the originals. HSP files are compact; there is little reason not to preserve them.

A good tool should use:

```text
video.hsp
video.hsp.bak
video.hsp.bak.2026-08-13T170400
```

or another rollback strategy.

---

# 4. Filename-driven projection and stereo detection

One of the easiest and most robust “mods” is simply naming files in a form HereSphere understands.

HereSphere's public FAQ documents filename suffixes that influence default projection/stereo/lens settings when opening a new video.

Suffixes must begin with an underscore, hyphen or space.

## 4.1 Stereo suffixes

| Suffix | Meaning |
|---|---|
| `_LR` | left-right / side-by-side |
| `_RL` | right-left / side-by-side with swapped eyes |
| `_3D` | left-right |
| `_TB` | top-bottom |
| `_BT` | bottom-top |
| `_3DV` | top-bottom |
| `_2D` | mono |

## 4.2 Equirectangular suffixes

| Suffix | Meaning |
|---|---|
| `_180` | equirectangular 180° |
| `_360` | equirectangular 360° |

If `_180` or `_360` exists without a stereo suffix, HereSphere treats the source as mono.

## 4.3 Fisheye / lens suffixes

| Suffix | Meaning |
|---|---|
| `_F180` or `_180F` | 180° fisheye, Linear lens, SBS |
| `_FISHEYE190` or `_RF52` | 190° fisheye, Linear lens |
| `_MKX200` | 200° iZugar MKX200 |
| `_MKX22` | 220° iZugar MKX22 |
| `_VRCA220` | 220° VRCA220 |

## 4.4 Equiangular cubemap

```text
_EAC360
_360EAC
```

## 4.5 Full-aspect-ratio stereo

The public FAQ notes that adding `F` to a stereoscopic label—such as `_LRF`—marks a source as two **full-aspect-ratio** stereo frames so HereSphere does not stretch it like conventional half-width SBS.

The current HSP v7 enum also contains `FullSideBySide` as a distinct stereo mode.

## 4.6 Why filename conventions are preferable to hard patches

Filename detection is:

- officially supported;
- portable across installs;
- easy to undo;
- easy to automate;
- independent of PAK/executable versions.

If a large library has consistent naming errors, a rename tool is often a better modification than editing the player.

---

# 5. Image presets and global/default image behavior

HereSphere supports image-setting presets directly in the UI.

The official FAQ describes the normal workflow:

1. Set the image controls as desired.
2. Click the **IMAGE** group header.
3. Open the preset dropdown.
4. Save over the first preset to make those settings the default for new videos.
5. Add additional presets for commonly used looks.

This is the supported way to change defaults such as saturation and contrast without editing files manually.

Public release notes also describe overwriting the first Image preset to change the default sharpening value for new videos.

## Why presets matter for modders

If the goal is:

- “make every new video slightly less saturated,”
- “use my preferred sharpen value,”
- “use a darker/brighter baseline,”

then an Image preset is **far more maintainable** than altering Unreal material defaults in the cooked PAK.

Use HSP profiles for **per-video** behavior and image presets for **new-video defaults**.

---

# 6. Per-video HSP profiles

HSP is HereSphere's native per-media profile format.

HSP profiles are the most powerful supported/semi-supported modification surface because they can store not only tags and resume metadata but also time-keyframed projection, image, lens, synchronization, autofocus, audio and environment settings.

The supplied 2024 layout shows a versioned `UMediaPreset` structure. The current Windows saved-data set inspected for this guide used **HSP version 7** records.

## 6.1 Top-level HSP data

Version 7 can contain top-level fields including:

```text
Version
UniqueID
Title
Description
DateAdded
DateReleased
DateLastPlayed
DateEdited
Duration
ResumeTime
ABLoopStartTime
ABLoopEndTime
PlayCount
Comments
Favorites
IsFavorite
AverageRating
AudioTrack
Tags
ProjectionKeyFrames
```

These are serialized application data, not JSON or INI text.

## 6.2 HSP is versioned

The 2024 loader explicitly checks the profile version and has version gates for newer fields. Examples:

- tag tracks: v3+
- audio keyframe group: v3+
- audio-track index: v4+
- shadows/midtones/highlights: v4+
- release date: v5+
- force-mono format flag: v6+
- AB-loop times: v7+
- environment keyframes: v7+

External tools must be version-aware.

## 6.3 Standalone `.hsp` wrapper versus embedded presets

A critical distinction established by native exports and saved-library parsing is that **standalone HSP files and embedded library presets do not begin at the same byte**.

A current standalone HSP, after decompression, begins with:

```text
int32 1024   # standalone outer marker
int32 7      # inner UMediaPreset/HSP version
...v7 payload
```

By contrast, presets embedded in `Library.hsl` begin directly at the inner media-preset version (`int32 7` in the validated dataset). They do **not** carry the standalone `1024` wrapper.

This matters because an embedded-style payload can look perfectly parseable to your tool and still fail to apply when handed to HereSphere as a standalone `.hsp`. Treat the container and the inner preset as separate layers.

---

# 7. What an HSP v7 profile can modify

The v7 projection/keyframe container has these independent groups:

```text
Format
Lens
Stitch
Alignment
Orientation
Origin
Motion
AutoFocus
Sync
Transition
Image
Audio
Environment
```

That means a profile can be deliberately minimal.

For example, a color-correction tool can include only an Image keyframe and leave lens/stitch/alignment untouched.

Likewise, a lens-calibration tool can modify Lens and Format data while preserving image corrections.

This separation is extremely useful for community tooling: **do not rewrite unrelated groups unless your tool owns them**.

---

# 8. Image correction fields in HSP v7

The 2024 `FImageKeyFrame` layout serializes:

```text
Time
Sharpen
ColorExposureBias
ColorWhiteTemp
ColorWhiteTint
ColorSaturation   (4 components)
ColorContrast     (4 components)
ColorGamma        (4 components)
ColorGain         (4 components)
ColorOffset       (4 components)
Shadows           (v4+)
Midtones          (v4+)
Highlights        (v4+)
```

The inspected current-data profiles establish these useful **neutral values** for the April 2025 Windows generation:

| Field | Neutral |
|---|---:|
| Sharpen | `2.0` |
| Exposure bias | `0.0` |
| White temperature | `6500.0` |
| White tint | `0.0` |
| Saturation | `(1,1,1,1)` |
| Contrast | `(1,1,1,1)` |
| Gamma | `(1,1,1,1)` |
| Gain | `(1,1,1,1)` |
| Offset | `(0,0,0,0)` |
| Shadows | `0.0` |
| Midtones | `1.0` |
| Highlights | `1.0` |

These values are valuable for generating a **neutral image-only HSP**.

### Verified four-component image-vector semantics

Native HereSphere exports establish that the fourth component (`W`) is the **global control**, while the first three components are independent RGB trims. For example:

- white balance uses `ColorGain.R/G/B`;
- global saturation is `ColorSaturation.W`;
- global contrast is `ColorContrast.W`;
- global gamma is `ColorGamma.W`;
- global brightness/offset is `ColorOffset.W`;
- the RGB components of Saturation/Contrast/Gamma/Offset can hold small per-channel nonlinear trims.

A native export with global contrast above neutral, for example, stores the change in the fourth component rather than multiplying all three RGB components. Tools should preserve this separation instead of flattening a four-component control into a scalar guess.

## 8.1 Practical image modifications

HSP image keyframes can be used to build tools for:

- white-balance correction;
- per-channel gain correction;
- brightness/offset adjustments;
- saturation normalization;
- contrast/gamma adjustment;
- sharpening defaults;
- time-varying correction for scenes with lighting changes.

## 8.2 Keyframed corrections

Because Image settings are a keyframe array, a profile does not have to use one correction for the entire movie.

A tool could theoretically create:

```text
00:00  neutral
12:32  warmer white balance
25:10  lower exposure
41:05  neutral again
```

Be conservative: aggressive time-varying image changes can be distracting, and a simple stable correction is often preferable.

---

# 9. Projection, stereo and lens calibration through HSP

The `FFormatKeyFrame` group can store:

```text
Time
ProjectionMode
StereoMode
EyeSwap
ForceMono              v6+
FlipFrontBack          v7+
FlipLeftRight          v7+
FlipUpDown             v7+
AspectRatioType
AspectRatio
Zoom
Pan
OrientationType        v1+
```

## 9.1 Projection enum in v7

| Value | Projection |
|---:|---|
| 0 | Perspective |
| 1 | Equirectangular |
| 2 | Fisheye |
| 3 | Equirectangular360 |
| 4 | Cubemap |
| 5 | EquiangularCubemap |

## 9.2 Stereo enum in v7

| Value | Stereo mode |
|---:|---|
| 0 | Mono |
| 1 | SideBySide |
| 2 | TopBottom |
| 3 | FullSideBySide |

## 9.3 Lens keyframes

A lens keyframe contains:

```text
Time
TrueLensName
ExportLensName
TrueLensCalibration
ExportLensCalibration
TrueFOV
ExportFOV
```

This is the correct modification layer for tools that calibrate:

- lens/FOV mismatch;
- headset-visible projection scale;
- camera/lens geometry;
- export vs true calibration values.

Do not try to solve lens calibration by reshaping the actual source video unless you have a specific transcoding use case. The native projection layer exists precisely so the player can do this non-destructively.

---

# 10. Stitching, alignment, orientation and origin

HSP contains several independent spatial-calibration systems.

## 10.1 Stitch keyframes

Each stitch keyframe contains four-component values for:

```text
Shift
Scale
Shear
Flare
Slant
```

This allows the player to correct stereo/lens stitching without modifying the video file.

## 10.2 Alignment keyframes

Alignment stores:

```text
Position
Rotation
```

## 10.3 Orientation keyframes

Orientation stores a rotation independently of the alignment group.

## 10.4 Origin keyframes

Origin stores a 3D position.

### Tool-design recommendation

If building a calibration editor, expose these as separate conceptual groups rather than flattening everything into a generic “offset.” That mirrors how HereSphere stores the values and makes preservation of unrelated settings easier.

---

# 11. Autofocus, motion and synchronization keyframes

## 11.1 Motion

Motion keyframes contain:

```text
Time
MotionDistance
```

## 11.2 Autofocus

Autofocus keyframes contain:

```text
Time
Rotation
FocalDistanceOverride
MinFocusDistance      v2+
MaxFocusDistance      v2+
FocusTransitionSpeed  v2+
```

The official FAQ notes that autofocus works best when projection settings are accurately calibrated. Low-contrast areas, bright-background occlusion and repeating/symmetrical patterns can reduce stability. Narrowing min/max focus distance can help reject poor matches.

HereSphere also exposes user-side autofocus accuracy/sensitivity settings in the current Windows binary.

## 11.3 Synchronization

Sync keyframes contain:

```text
Time
StartingTicksOffset
TicksPerHourOffset
```

This provides both a starting offset and a drift-per-hour correction.

It is a much better basis for long-video synchronization tools than applying one hardcoded delay to the entire source.

---

# 12. Audio, transitions and environment keyframes

## 12.1 Transition keyframes

Transitions contain:

```text
Time
Type
Duration
```

The supplied HSP layout references the transition enum but does not include its complete enum definition. Community tools should not invent numeric names for unknown transition types.

## 12.2 Audio keyframes

Audio keyframes (v3+) contain:

```text
Time
TimeOffset
VolumeMultiplier
```

This enables per-video/per-time audio timing and volume modifications.

## 12.3 Environment keyframes

Environment is a v7+ group. It stores:

```text
Time
BackgroundType
BackgroundColor
BackgroundName
MaskType
OpacityScale
ChromaKey1Color
ChromaKey1Settings
ChromaKey2Color
ChromaKey2Settings
ChromaKey3Color
ChromaKey3Settings
DespillColor
DespillSettings
LightColor
AlphaCoordinates
```

The v7 enum contains:

### Background

```text
GlobalSetting
Color
Passthrough
```

### Mask type

```text
None
ChromaKey
AlphaPacked
```

This is the HSP-level customization surface for passthrough/chroma/alpha-packed behavior available in that generation.

External-alpha support was added later in HereSphere v0.12.0 and therefore postdates the supplied 2024 v7 layout.

---

# 13. Tags, ratings, favorites and resume metadata

HSP is not only projection data.

A profile can also contain:

- title and description;
- date metadata;
- resume time;
- AB-loop start/end (v7+);
- play count;
- comments/favorites counts;
- favorite state;
- average rating;
- selected audio track;
- timestamped tags.

## 13.1 HSP tags

Each serialized tag contains:

```text
Tag
Rating
Start
End
Track          v3+
```

This makes HSP a useful local/offline metadata interchange format for community organizers, tag editors and bulk catalog tools.

## 13.2 Avoid confusing HSP metadata with source truth

If the same media is also managed by a separate catalog/server, decide explicitly which side owns:

- favorite state;
- rating;
- title;
- tags;
- resume position.

Blind two-way synchronization can easily create loops or overwrite newer edits.

---

# 14. Editing HSP files safely

HSP files are compressed binary Unreal-serialized data. They are **not** safe to edit with a hex editor unless you fully understand the layout and string/array lengths.

## 14.1 Recommended parser behavior

A community HSP editor should:

1. Read the version first.
2. Reject or preserve versions newer than it understands.
3. Parse every count with sanity limits.
4. Preserve unknown data where possible.
5. Validate all lengths before allocating memory.
6. Re-parse its own output before replacing the original file.
7. Write atomically (`temporary file → fsync → rename`).
8. Keep an original backup.

## 14.2 Do not “upgrade” unknown versions

If a future HereSphere release writes HSP v8 and your editor only knows v7:

**Do not** load the beginning, rewrite what you know as v7, and silently discard the rest.

A safer tool should say:

```text
This profile is HSP v8. This tool understands through v7.
Read-only / preserve-original mode only.
```

## 14.3 Minimal-profile strategy

When creating a new profile for one purpose, include only the keyframe groups you actually control.

Examples:

- image correction → Image only;
- lens calibration → Format + Lens only;
- audio sync → Audio/Sync only;
- environment preset → Environment only.

This minimizes collateral changes.

---

# 15. External alpha videos and passthrough customization

HereSphere v0.12.0 added support for a separate video controlling passthrough opacity.

For local media, name the alpha video the same as the main video and append:

```text
_XALPHA
```

immediately before the extension.

Example:

```text
scene_MKX200.mp4
scene_MKX200_XALPHA.mp4
```

Public release notes describe the alpha interpretation as:

- black → transparent;
- colored portions → opaque;
- opacity is based on the sum of RGB channels in linear color space.

The developer notes that approximately **480p can be sufficient** for an alpha video and recommends matching keyframe spacing with the main video so seeking stays aligned.

## 15.1 Why this is a useful modification surface

External alpha makes it possible to create custom passthrough experiences without changing the main video:

- foreground cutouts;
- custom masks;
- improved subject isolation;
- stylized transparency regions.

Keep the alpha source timebase/GOP structure aligned with the original whenever possible.

---

# 16. Scripts and synchronized peripherals

HereSphere supports synchronized peripheral scripts and has a dedicated synchronized-peripherals menu.

Public release notes and the current binary expose features such as:

- script selection;
- connection keys;
- time offsets;
- timestamp offsets;
- heatmap generation/opacity;
- fallback script directories;
- direct-control peripheral actions.

## 16.1 Local sidecar scripts

Where supported, keeping scripts beside or predictably associated with media is more maintainable than modifying player code.

## 16.2 Script fallback directory

HereSphere has supported a fallback script directory for streamed video. Public release notes describe a helper button that writes a sanitized streamed-video title so users can rename a fallback script appropriately.

## 16.3 Timing modifications

For sync correction, prefer player/script time-offset controls or HSP Sync/Audio fields rather than destructively shifting media timestamps.

---

# 17. Subtitles and subtitle-related customization

The Windows install ships:

```text
libass
FreeType
FriBidi
HarfBuzz
libiconv
```

which is consistent with HereSphere's external subtitle support and text shaping/rendering stack.

The player supports common ASS/SSA/SRT-style subtitle workflows and exposes subtitle settings including current-build strings for:

```text
subtitle enable/disable
flat-screen depth offset
pitch
fixed distance
autofocus scale
autofocus enable
```

## 17.1 Better modification strategy

If the goal is styling or timing subtitles:

1. Prefer subtitle-format styling (especially ASS) where supported.
2. Use HereSphere subtitle position/depth settings.
3. Fix timing in the subtitle file or via media/audio timing controls.
4. Avoid replacing subtitle-rendering DLLs unless debugging a renderer problem.

Replacing `libass`/FreeType/HarfBuzz with arbitrary newer builds can introduce ABI/runtime incompatibilities.

---

# 18. Audio tracks and audio timing

HereSphere v0.12.0 added audio-track switching when a video has multiple labeled-language tracks.

For media preparation:

- label tracks with meaningful language metadata;
- keep codec/container choices compatible with the target headset/player;
- use HSP Audio keyframes or player audio offsets when timing needs correction.

The current Windows binary also exposes:

```text
GlobalAudioTimeOffset
KeyFrameAudioTimeOffset
TotalAudioTimeOffset
```

which reinforces that audio synchronization is designed as a player-side adjustment rather than requiring a remux for every timing issue.

---

# 19. Keybindings inside HereSphere

HereSphere supports configurable bindings for several device classes. Current-build strings include separate menus for:

```text
Motion Controller
Gamepad
Keyboard
Mouse
```

and functions for:

```text
LoadSavedKeyBindings
WriteSavedKeyBindings
DefaultKeyBindings
GenerateKeyBindings
```

Public release notes added keyboard/mouse support and recommend using the in-player keybindings menu/default bindings.

## 19.1 Prefer the UI first

For ordinary users and mod packs, prefer:

> HereSphere keybindings UI → export/save → share instructions

rather than hand-editing opaque saved keybinding data.

## 19.2 Saved keybinding file

The inspected user-data archive contains:

```text
Default/KeyBindings.hsk
```

This is an application-level saved keybinding file. Treat it as binary/version-sensitive unless a complete current parser is available.

---

# 20. SteamVR binding files

The Windows install contains loose SteamVR binding JSON files under:

```text
HereSphere/Config/SteamVRBindings/
```

Observed files include:

```text
gamepad.json
holographic_controller.json
indexhmd.json
knuckles.json
oculus_touch.json
rift.json
steamvr_manifest.json
vive.json
vive_controller.json
vive_cosmos_controller.json
vive_pro.json
vive_tracker_camera.json
```

These are much more approachable than native code.

## 20.1 What they are useful for

SteamVR binding files can define/default-map actions for specific controller profiles.

Potential community uses include:

- alternative default layouts;
- accessibility-oriented bindings;
- one-handed layouts;
- controller-specific convenience actions;
- documented binding presets for unusual hardware.

## 20.2 Preferred workflow

Whenever possible:

1. Create/test the binding through SteamVR's binding interface.
2. Export/share the binding through supported SteamVR mechanisms.
3. Only hand-edit JSON when you understand the action manifest and device schema.

## 20.3 Steam update caveat

Files inside the application installation can be replaced by Steam verification/updates.

Do not make your community mod installer assume those JSON files will remain byte-identical forever.

---

# 21. Saved data: library, settings, backups and keybindings

The supplied saved-data archive exposed these file families:

```text
Backup*.hsb
KeyBindings.hsk
Library.hsl
```

## 21.1 `Library.hsl`

The saved library contains media-preset records and is likely the most important persistent data file for a local library. Current-data analysis found large numbers of valid HSP-v7-style media-preset records inside the saved library generation used for validation.

Use cases for tooling include:

- backup/restore;
- read-only library inventory;
- migration reports;
- detecting duplicate/stale entries;
- extracting metadata for recovery.

Writing the library directly is substantially riskier than generating standalone HSP files.

The validated `Library.hsl` records begin at the inner HSP/`UMediaPreset` version. Do not prepend the standalone `1024` wrapper when parsing or reconstructing embedded records; that marker belongs to standalone `.hsp` files.

## 21.2 `Backup*.hsb`

These are application backups. They are valuable recovery artifacts.

A modding tool should **never delete them automatically** just because it believes the main library is valid.

## 21.3 `KeyBindings.hsk`

This contains saved application keybindings.

## 21.4 These are not ordinary text files

Do not treat `.hsl`, `.hsb`, or `.hsk` like INI/JSON just because the extension looks simple. The inspected files are binary/compressed application data.

For community tools, read-only inspection should come before write support.

---

# 22. The Unreal `.pak` package

The inspected Windows installation contains one primary cooked game package:

```text
HereSphere/Content/Paks/HereSphere-WindowsNoEditor.pak
```

The supplied file is about 125 MB.

Its footer contains the standard Unreal PAK magic (`0x5A6F12E1`) and **PAK version 9**.

That means normal Unreal PAK tooling that understands this generation can at least inspect the container structure.

## 22.1 What normally lives in a cooked PAK

Depending on how the project was cooked, a PAK can contain assets such as:

- UMG/UI widget assets;
- materials and material instances;
- textures;
- data assets/tables;
- cooked Blueprints;
- sounds;
- maps;
- shader-related/cooked resources;
- default objects/config embedded in cooked assets.

The exact set must be established by listing/extracting the package rather than assumed.

## 22.2 PAK is not source code

Cooked assets are not equivalent to the original Unreal project.

You usually do **not** get:

- original `.uasset` authoring context;
- clean C++ source;
- original Blueprint graphs in an editable project form;
- source PSDs/models/textures;
- developer comments.

Asset extraction is useful for understanding structure, but re-authoring cooked assets can be difficult.

---

# 23. What PAK modding can and cannot realistically do

## 23.1 Reasonable experiments

Depending on the cooked asset type and available tooling, PAK experiments may be useful for:

- inspecting UI/widget hierarchy;
- discovering material/shader assets;
- identifying default data assets;
- replacing a simple texture/resource for personal testing;
- learning which features are Blueprint/data-driven vs native C++.

## 23.2 Harder modifications

These are much more fragile:

- changing cooked Blueprint logic;
- changing UMG widget behavior;
- adding entirely new native gameplay/player systems;
- changing decoder logic;
- changing rendering code compiled into native modules.

## 23.3 Do not distribute the original PAK

A public mod should distribute **only your original patch/delta/assets/instructions**, not HereSphere's copyrighted game package or extracted proprietary assets.

## 23.4 Expect updates to overwrite it

Steam updates can replace the entire PAK.

Keep patches version-specific and re-verify them after every update.

---

# 24. Embedded Chromium / BLUI

The Windows installation includes the BLUI Unreal plugin and a Chromium Embedded Framework runtime under:

```text
HereSphere/Plugins/BLUI/ThirdParty/cef/Win/shipping/
```

Files include:

```text
blu_ue4_process.exe
libcef.dll
chrome_elf.dll
libEGL.dll
libGLESv2.dll
cef.pak
cef_extensions.pak
devtools_resources.pak
icudtl.dat
v8_context_snapshot.bin
locales/*.pak
```

This is the embedded browser stack, not the VR video renderer itself.

## 24.1 What CEF modifications affect

Potentially:

- browser rendering/runtime behavior;
- Chromium localization/resources;
- web compatibility;
- browser process startup.

They do **not** automatically modify native HereSphere video projection/image correction.

## 24.2 Replacing CEF is high-risk

Do not casually swap `libcef.dll` for a newer Chromium build.

BLUI, helper processes, resource PAKs, V8 snapshots and CEF binaries are version-coupled. A partial update can fail at startup or crash the browser process.

Public HereSphere releases have updated browser user-agent behavior from application code without requiring users to replace the CEF runtime manually.

## 24.3 Browser debugging

The included `debug.log` shows Chromium/CEF-style errors and warnings, including media-demuxer and SPDY/HTTP stream messages. That log can be valuable when debugging:

- web-page preview media;
- embedded-browser playback;
- browser networking;
- CEF crashes.

Do not assume every CEF log entry describes the main VR video decoder.

---

# 25. Native DLLs and the shipping executable

The main Windows binary is:

```text
HereSphere/Binaries/Win64/HereSphere-Win64-Shipping.exe
```

The inspected build is approximately 72.6 MB and dated 2025-04-30.

Static strings/reflection metadata reveal many application systems, including:

```text
projection/stereo modes
lens presets
image controls
autofocus
keybindings
subtitles
favorites/ratings
environment/passthrough
synchronized peripheral timing
Steam/Web API logic
```

## 25.1 There is no observed traditional native plugin ABI

The install does not expose an obvious “drop DLL plugins here” SDK/ABI for arbitrary third-party native extensions.

Do not assume that because HereSphere uses Unreal plugins internally (`BLUI`, etc.) an end user can simply compile a new Unreal plugin and drop it into the shipping build.

Cooked Unreal applications generally expect plugins to be present and integrated at build/cook time unless the application intentionally implements dynamic plugin loading.

## 25.2 Binary patching is possible but fragile

Research techniques can include:

- string inspection;
- PE import/export inspection;
- Unreal reflection-name inspection;
- call/reference analysis in a disassembler;
- controlled runtime debugging;
- comparing two application versions.

But a hardcoded patch at one RVA/byte pattern can fail on the next compiler build.

## 25.3 DLL replacement caveat

The installation includes third-party runtime DLLs for subtitles, audio/engine functionality, OpenVR, NVIDIA Aftermath, etc.

Replacing these to “upgrade” them is usually a poor mod strategy because:

- ABI compatibility is not guaranteed;
- the app may expect exact behavior;
- Steam may restore them;
- crashes can be difficult to distinguish from application bugs.

Prefer external/sidecar tools where possible.

---

# 26. Debug logs and troubleshooting

The Windows build includes:

```text
HereSphere/Binaries/Win64/debug.log
```

The supplied log contains repeated Chromium media pipeline errors such as:

```text
FFmpegDemuxer: no supported streams
PIPELINE_ERROR DEMUXER_ERROR_NO_SUPPORTED_STREAMS
```

as well as Chromium/SPDY warnings and shader warnings.

## 26.1 What the log is useful for

- embedded-browser media failures;
- malformed browser preview streams;
- CEF renderer/process issues;
- networking protocol anomalies;
- shader/compiler warnings in embedded content.

## 26.2 What it may not explain

Do not assume a browser `FFmpegDemuxer` error means HereSphere's primary VR media playback pipeline cannot decode the same file. The log belongs to a build containing an embedded Chromium browser, which has its own media stack and codec restrictions.

## 26.3 Preserve timestamps and reproduction steps

When sharing a problem with developers:

```text
HereSphere version/build
Windows/Quest version
GPU/headset
source type: local / SMB / web / API
file codec/container/resolution
exact action before error
relevant log timestamp
```

is far more useful than posting the entire log without context.

---

# 27. A safe PAK inspection workflow

If you want to investigate cooked content, use a read-first workflow.

## Step 1 — record hashes

```powershell
Get-FileHash .\HereSphere-WindowsNoEditor.pak -Algorithm SHA256
```

## Step 2 — copy the PAK

Never experiment directly on your only installed copy.

## Step 3 — identify PAK generation

The inspected package footer indicates Unreal PAK version 9.

Use tools compatible with that generation.

## Step 4 — list before extracting

Start with an index listing. It answers many questions without copying every asset.

## Step 5 — extract to a separate research directory

Do not extract over the Steam installation.

## Step 6 — classify assets

Separate:

```text
UI/widgets
materials
textures
data assets
maps
Blueprint/cooked logic
misc resources
```

## Step 7 — make one controlled change

Do not change five systems at once.

## Step 8 — rebuild/overlay only if your tooling supports the exact format

If the game rejects the modified PAK, restore the original immediately.

## Step 9 — document version and hashes

A PAK mod should say exactly which HereSphere build it targets.

---

# 28. A safe binary-inspection workflow

Native analysis should also start read-only.

## 28.1 Useful non-destructive checks

```text
PE architecture
file version/timestamp
imports
exports
embedded strings
Unreal reflection/class/function names
resource table
compiler/runtime hints
```

## 28.2 Search for feature names, not magic offsets

Feature-related strings such as:

```text
ColorGain
ColorGamma
ColorOffset
ColorSaturation
AutoFocusAccuracy
DefaultKeyBindings
FullSideBySide
ChromaKey
AlphaPacked
```

can help orient a reverse-engineer.

But a string's presence is **not proof** of an externally supported setting or stable function address.

## 28.3 Keep observations separate from claims

Good community documentation says:

```text
Observed in Windows build dated 2025-04-30
```

instead of:

```text
This is guaranteed forever.
```

---

# 29. Steam updates and keeping mods maintainable

Steam updates can replace:

- EXE files;
- PAK files;
- loose bindings/config packaged with the app;
- DLLs.

User-generated/saved data is a separate concern and should still be backed up independently.

## 29.1 Mod manifest recommendation

Every distributable mod should have a small manifest/readme containing:

```text
mod name/version
HereSphere version/build tested
files modified
original file hashes
modified file hashes
rollback steps
known conflicts
```

## 29.2 Prefer patchers over redistributed binaries

If a binary/PAK patch is unavoidable, distribute a patch/delta or patching script—not the original proprietary file.

## 29.3 Verify after updates

Never blindly reapply an old binary patch after HereSphere updates.

At minimum:

1. compare hashes;
2. verify target patterns still match uniquely;
3. re-test the behavior;
4. update documentation.

---

# 30. Useful modification recipes

These examples use the least-invasive layer available.

## 30.1 “This video opens with the wrong stereo format”

Best options, in order:

1. Correct the filename suffix (`_LR`, `_RL`, `_TB`, etc.).
2. Save the correct per-video setting/HSP.
3. Batch-edit HSP Format keyframes with a version-aware tool.
4. **Do not patch the EXE's autodetection algorithm** unless there is a genuine player bug affecting many files.

## 30.2 “I want all new videos to use my preferred image look”

Use the first **IMAGE preset** as the new-video default.

Do not alter material constants in the PAK.

## 30.3 “I want one video to have corrected white balance”

Use an HSP Image keyframe.

Keep Format/Lens/Stitch groups untouched.

## 30.4 “I want different correction in different parts of a video”

Create multiple Image keyframes at the transition times.

## 30.5 “The video slowly drifts out of sync”

Use HSP Sync data (`StartingTicksOffset`, `TicksPerHourOffset`) or relevant synchronized-peripheral timing controls.

## 30.6 “Audio is offset but the video is otherwise correct”

Use audio timing controls/HSP Audio keyframes before remuxing the entire source.

## 30.7 “I want a custom VR controller layout”

Use HereSphere's keybinding UI or SteamVR bindings.

Only modify application-shipped SteamVR JSON directly when building a controlled version-specific preset.

## 30.8 “I want passthrough around a subject”

Use chroma/alpha-packed Environment settings, or create an `_XALPHA` sidecar on v0.12.0+.

Do not modify the video renderer binary simply to create a mask.

## 30.9 “I want to change HereSphere's UI artwork/layout”

This is PAK/cooked-asset territory.

Expect more work and more update breakage than HSP/input mods.

## 30.10 “I want to add a completely new native feature inside HereSphere”

There is no observed public drop-in native plugin SDK. Prefer a companion application, supported protocol, sidecar data, or external process unless native binary research is the actual goal.

---

# 31. Things that look moddable but usually should not be modified

## Chromium resource PAKs

`cef.pak` and related files belong to the embedded Chromium runtime. They are not the main HereSphere content PAK.

## `libcef.dll`

Do not swap it independently from its matching CEF resources/helper process.

## Subtitle DLLs

`libass`, FreeType, FriBidi and HarfBuzz are version-coupled native libraries. Updating one in isolation can break rendering or ABI expectations.

## Engine runtime DLLs

PhysX/APEX/OpenVR/etc. are part of the packaged Unreal runtime. Replacing them is not a normal mod path.

## The root `HereSphere.exe`

The small root executable is not the same thing as `HereSphere-Win64-Shipping.exe`. The shipping binary is where most compiled application code lives.

## Backup files

Do not delete `.hsb` backups merely to “clean up” saved data.

---

# 32. Current-build observations and version caveats

This guide combines multiple generations of information.

## 32.1 Inspected Windows build

The supplied Windows Shipping executable is dated:

```text
2025-04-30
```

The install contains the UE4 runtime, SteamVR binding JSON, subtitle libraries, BLUI/CEF browser runtime and a single primary `HereSphere-WindowsNoEditor.pak`.

## 32.2 HSP layout

The supplied 2024 layout documents HSP through version 7. The April 2025-era saved-media data validated for this work also uses version 7 media-preset structures.

## 32.3 Newer public features

Public Quest release notes continued after that PC build, including:

- v0.11.9 — June 2025;
- v0.11.10 — July 2025;
- v0.11.11 — October 2025;
- v0.11.12 — November 2025;
- v0.12.0 — January 2026.

External alpha and audio-track switching are examples of features newer than the inspected April 2025 PC executable.

Do not use one platform/build date as proof that every Quest/Steam version has identical internals.

---

# 33. Recommended community-tool architecture

For most modification ideas, the most maintainable architecture is **external tooling around stable data surfaces**, not injection into the player.

A community utility can safely focus on things like:

```text
Media files
   │
   ├── filename convention manager
   ├── subtitle/script sidecars
   ├── external alpha generation
   │
   └── HSP profiles
          ├── metadata editor
          ├── image correction
          ├── format/lens calibration
          ├── audio/sync editor
          └── environment editor
```

Benefits:

- player updates are less likely to break the tool;
- no executable patching;
- easy backups;
- works with local/SMB libraries;
- modifications are transparent and reversible;
- tools can be open sourced without redistributing HereSphere code/assets.

Native/PAK research still has value for **understanding** the player, discovering file formats and validating behavior—but that does not mean every discovery should become a binary patch.

---

# 34. Source notes and public references

## Source categories used for this guide

### Community developer material

- 2024 HereSphere HSP serialization layout shared through the HereSphere Discord developer community.
- The separate HereSphere JSON API documentation was used only for cross-referencing terminology; Web API implementation details belong in the dedicated API references, not this document.

### Inspected Windows installation

The guide was validated against a supplied Windows/Steam installation inventory and static artifacts including:

```text
HereSphere-Win64-Shipping.exe     2025-04-30 generation
HereSphere-WindowsNoEditor.pak    UE4 PAK v9 footer observed
Manifest_NonUFSFiles_Win64.txt
saved application data archive
debug.log
```

No proprietary executable, PAK, saved library, or extracted game asset is redistributed with this document.

### Official public HereSphere documentation/release notes

- FAQ / filename conventions / presets / autofocus:  
  https://heresphere.com/faq/

- v0.7.4 — sharpening/default Image preset reminder:  
  https://heresphere.itch.io/heresphere-vr-video-player-quest-2/devlog/473049/update-v074-released

- v0.9.5 — Auto Create HSP, Toggle Favorite, browser/network notes:  
  https://heresphere.itch.io/heresphere-vr-video-player-quest-2/devlog/576641/update-v095-released

- v0.10.0 — passthrough, chroma key, alpha packed, keyboard/mouse:  
  https://heresphere.itch.io/heresphere-vr-video-player-quest-2/devlog/635013/update-v0100-released

- v0.12.0 — external alpha and audio-track switching:  
  https://heresphere.itch.io/heresphere-vr-video-player-quest-2/devlog/1318258/update-v0120-released

- Current public download page (newer 0.12.x packages may appear before detailed devlogs):  
  https://heresphere.itch.io/heresphere-vr-video-player-quest-2

## Community-documentation rule of thumb

When adding findings to this guide, label them as one of:

```text
Official/documented
Observed in build <date/version>
Derived from HSP layout v<version>
Experimental/inferred
Unknown
```

“Unknown” is better than inventing a stable behavior from one binary string or one old build.

---

# Closing note

HereSphere is unusually friendly to **data-driven modification** even without a formal native plugin SDK. Filename conventions, HSP keyframes, image presets, bindings, sidecar scripts/subtitles, passthrough masks and external alpha cover a large amount of practical customization.

The deeper Unreal/PAK/native layers are still useful for research, but the best community modifications will usually be the ones that **leave the player binary untouched** and operate through the persistent formats HereSphere already knows how to consume.
