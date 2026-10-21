# NK Combine & Bake

NK Combine & Bake is a Blender add-on designed to combine selected mesh objects and bake their materials into texture maps, simplifying complex assets for game development and other 3D workflows.

## Features

### Mesh Combining
- Combines selected mesh objects into a single output object.
- Applies evaluated geometry, including the results of modifiers.
- Preserves world-space transforms when combining objects.
- Optionally hides the original objects after processing.

### UV Atlas Generation
- Creates a unified UV map named `AtlasUV`.
- Supports two UV layout methods:
  - **Repack Original UVs** — reuses existing UV layouts where possible, repairs faces with degenerate UVs, and packs UV islands.
  - **Smart UV Project** — generates a new UV layout using Blender's Smart UV Project.
- Provides controls for UV island margins and the Smart UV angle.

### Material Baking
Bakes selected material channels into texture maps using Cycles:

- Base Color
- Metallic
- Roughness
- Normal
- Emission
- Alpha

Each channel can be enabled or disabled independently.

### Texture Resolution and Quality
- Supports texture resolutions of 512, 1024, 2048, 4096, and 8192 pixels.
- Allows adjustment of Cycles bake samples.
- Provides control over the bake margin to help reduce visible seams.

### Material and Texture Organization
- Creates simplified materials using the baked textures.
- Supports splitting the output into multiple material groups to distribute texture resolution across complex assets.
- Groups source material slots approximately by surface area.
- Supports saving textures as PNG files to a selected directory or packing them into the current Blender file.

### Diagnostics
- Prints diagnostic information to the Blender console.
- Generates a text report named `NK_Bake_Report` inside Blender.

## How to Use

1. Select the mesh objects you want to combine.
2. Open the 3D Viewport sidebar by pressing `N`.
3. Select the **NK Bake** tab.
4. Configure the output name, texture resolution, material groups, samples, bake margin, and UV layout.
5. Select the material channels to bake.
6. Choose whether to hide the original objects and where to save the textures.
7. Click **Combine & Bake**.

## Requirements

- Blender 4.5.0 or newer.
- Cycles rendering engine for baking.

## Author

NK