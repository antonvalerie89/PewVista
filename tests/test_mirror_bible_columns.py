"""Regression tests for Bible overlay fitting and mirror payload signatures."""

from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import time
import threading
import tkinter as tk
from tkinter import ttk
from unittest.mock import Mock, patch
import unittest

from tab_display import build_bible_preview_signature, build_display_tab, preload_bible_payloads, should_render_bible_columns_in_display_details
from mirror import MirrorDisplayController


class PreviewMediaRegressionTests(unittest.TestCase):
    """What: Run preview media checks in unittest-based build gates.

    Why included: Module-level test functions otherwise do not run during builds.
    Where used: Desktop release preview regression discovery.
    Returns: unittest results for image framing and animated media.
    """

    def test_preview_media_fitting_playback_and_cleanup(self):
        """What: Check empty previews, portrait fitting, animation, and resource cleanup.

        Why included: Releases must not crop portraits or substitute black MP4 frames.
        Where used: Desktop builder's preview regression gate.
        Returns: None; invokes isolated image/GIF/video/browser checks.
        """
        test_preparation_preview_decodes_video_and_preserves_images()
        test_mirror_portrait_background_fits_and_landscape_still_crops()
        test_secondary_image_fit_tracks_portrait_and_landscape()
        test_mirror_video_frames_advance_loop_and_release_reader()
        test_welcome_video_preview_animates_and_closes_on_destroy()

    def test_music_license_settings_and_legacy_song_migration(self):
        """What: Persist church license numbers and migrate old song rows safely.

        Why included: Existing Library databases must gain provider attribution without losing songs.
        Where used: Music licensing persistence regression discovery.
        Returns: None; checks settings round-trip and song provider insert/update.
        """
        import sqlite3
        import tab_library
        from music_licensing import load_music_license_settings, save_music_license_settings

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            data_directory = Path(directory)
            database = data_directory / "library.db"
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE songs (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, "
                "song_key TEXT NOT NULL DEFAULT '', verses_json TEXT NOT NULL DEFAULT '[]', "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, background_path TEXT NOT NULL DEFAULT '', "
                "font_settings_json TEXT NOT NULL DEFAULT '{}')"
            )
            connection.execute("INSERT INTO songs (title, created_at, updated_at) VALUES ('Legacy', 'now', 'now')")
            connection.commit()
            connection.close()
            opened_connections = []
            original_connect = tab_library.connect_library_db

            def tracked_connect():
                """What: Open a Library database connection and record it for later cleanup checks.

                Why included: The test verifies that every connection the Library tab opens is closed again.
                Where used: Patched connect_library_db in the Library connection regression test.
                Returns: The opened connection.
                """
                opened = original_connect()
                opened_connections.append(opened)
                return opened

            try:
                with patch.object(tab_library, "get_library_db_path", return_value=database), \
                        patch.object(tab_library, "get_song_pack_directories", return_value=[]), \
                        patch.object(tab_library, "connect_library_db", side_effect=tracked_connect):
                    tab_library.ensure_library_schema()
                    songs = tab_library.load_all_songs()
                    self.assertEqual(songs[0]["title"], "Legacy")
                    self.assertEqual(songs[0]["license_provider"], "")
                    song_id = tab_library.save_song_record(None, "New", "C", ["Verse"], "", "onelicense", "Ada Lovelace")
                    saved_song = next(song for song in tab_library.load_all_songs() if song["id"] == song_id)
                    self.assertEqual(saved_song["license_provider"], "OneLicense")
                    self.assertEqual(saved_song["author"], "Ada Lovelace")
                    tab_library.save_song_record(song_id, "New", "C", ["Verse"], "", "CCLI", "Grace Hopper")
                    saved_song = next(song for song in tab_library.load_all_songs() if song["id"] == song_id)
                    self.assertEqual(saved_song["license_provider"], "CCLI")
                    self.assertEqual(saved_song["author"], "Grace Hopper")
            finally:
                for opened in opened_connections:
                    opened.close()
            save_music_license_settings({"ccli": " 12345 ", "onelicense": "OL-88", "initial_prompt_completed": True}, data_directory)
            self.assertEqual(load_music_license_settings(data_directory), {
                "ccli": "12345", "onelicense": "OL-88", "initial_prompt_completed": True,
            })

    def test_display_queue_preserves_music_license_provider(self):
        """What: Keep the Library license provider through queue normalization.

        Why included: Live output and saved-service recall need the per-song provider metadata.
        Where used: Display queue normalization regression discovery.
        Returns: None; checks canonical provider values and legacy queue compatibility.
        """
        from display_bridge import _normalize_queue_item

        queue_item = _normalize_queue_item({
            "source": "library", "title": "Licensed Song", "text": "Verse",
            "license_provider": "onelicense", "license_author": "Ada Lovelace",
        })
        self.assertEqual(queue_item["license_provider"], "OneLicense")
        self.assertEqual(queue_item["license_author"], "Ada Lovelace")
        legacy_item = _normalize_queue_item({"source": "library", "title": "Old Song", "text": "Verse"})
        self.assertNotIn("license_provider", legacy_item)

    def test_live_music_license_corner_tracks_changed_number(self):
        """What: Refresh the live song corner when shared license numbers change.

        Why included: The menu editor must update the current song without resending lyrics.
        Where used: Mirror license-attribution regression discovery.
        Returns: None; verifies current number and signature invalidation.
        """
        controller = MirrorDisplayController(
            tk.Tk(), get_app_base_path=lambda: Path("."), get_display_config=lambda: {},
            get_preview_size=lambda: (320, 180), resolve_background_path=lambda path: path,
            serialize_overlay_font=lambda font: None, build_hot_background_paths=lambda paths: [],
            font_value_factory=lambda: ("Segoe UI", 18, "normal"),
            get_music_licenses=lambda: dict(self.license_settings),
        )
        controller.root.withdraw()
        payload = {"source": "library", "text": "Verse", "preview_key": "C", "license_provider": "CCLI", "license_author": "Ada Lovelace"}
        self.license_settings = {"ccli": "12345", "onelicense": "OL-88"}
        try:
            controller.set_live_payload(payload)
            self.assertEqual(controller.get_live_corner_text(), "C\nAuthor: Ada Lovelace\nCCLI License: 12345")
            first_signature = controller.build_live_payload_signature(payload)
            self.license_settings["ccli"] = "98765"
            self.assertNotEqual(first_signature, controller.build_live_payload_signature(payload))
            controller.refresh_music_license_attribution()
            self.assertEqual(controller.get_live_corner_text(), "C\nAuthor: Ada Lovelace\nCCLI License: 98765")
            controller.set_live_payload({"source": "library", "text": "Verse", "preview_key": "C", "title": "Song without license"})
            self.assertEqual(controller.get_live_corner_text(), "C")
            self.assertFalse(controller.show_song_metadata)
        finally:
            controller.close()
            controller.root.destroy()

    def test_key_size_scales_and_song_details_are_two_pixels_smaller(self):
        """What: Scale Key Size consistently and render attribution two pixels smaller.

        Why included: The menu size must match preview/output proportions while retaining readable hierarchy.
        Where used: Live Screen Key Size renderer regression discovery.
        Returns: None; checks sub-8 px values, output scaling, and actual Pillow font choices.
        """
        import overlay_layout

        prior_size = overlay_layout.DISPLAY_KEY_SIZE
        try:
            overlay_layout.set_display_key_size(12)
            self.assertEqual(overlay_layout.get_display_key_size(1920, 1080), 72)
            self.assertEqual(overlay_layout.get_corner_detail_size(72, 1920, 1080), 60)
            overlay_layout.set_display_key_size(4)
            self.assertEqual(overlay_layout.get_display_key_size(320, 180), 4)
            self.assertEqual(overlay_layout.get_display_key_size(1920, 1080), 24)
            self.assertEqual(overlay_layout.get_corner_detail_size(24, 1920, 1080), 12)
            overlay_layout.set_display_key_size(0)
            self.assertEqual(overlay_layout.DISPLAY_KEY_SIZE, 0)
            self.assertTrue(overlay_layout.is_display_key_hidden())
            self.assertEqual(overlay_layout.get_display_key_size(1920, 1080), 72)
            self.assertEqual(overlay_layout.get_corner_detail_size(12, 1920, 1080), 6)

            corner_layout = overlay_layout.OverlayLayout(
                text="", font_value=("Segoe UI", 18, "normal"), line_spacing=6,
                fill_color="#ffffff", stroke_color="#000000",
                corner_font_value=("Segoe UI", 14, "normal"),
            )
            with patch.object(overlay_layout, "get_pil_font", wraps=overlay_layout.get_pil_font) as load_font:
                image = overlay_layout.render_overlay_image(
                    640, 360, corner_layout,
                    corner_text="C\nSong title\nAuthor: Ada\nCCLI License: 12345",
                )
            self.assertIsNotNone(image.getbbox())
            corner_font_sizes = [call.args[0][1] for call in load_font.call_args_list[-2:]]
            self.assertEqual(corner_font_sizes, [14, 10])
        finally:
            overlay_layout.set_display_key_size(prior_size)

    def test_library_song_license_selector_and_live_send(self):
        """What: Verify Library exposes both providers and sends chosen attribution live.

        Why included: Each song's provider must persist into Display without entering lyric text.
        Where used: Library editor and mirror regression discovery.
        Returns: None; checks selector values, live corner text, and queue metadata.
        """
        test_library_show_live_activates_selected_song_in_both_outputs()

    def test_bible_pagination_combines_complete_verses(self):
        """What: Pack whole Bible verses into synchronized translation pages.

        Why included: Display should fill pages without splitting a verse or losing its order.
        Where used: Bible pagination regression discovery.
        Returns: None; checks grouped verses, translation boundaries, and oversized verses.
        """
        import copy
        from overlay_layout import paginate_bible_columns

        verses = [f"{number}. Complete verse text. " for number in range(1, 11)]
        columns = [
            {"title": "KJV", "verses": verses},
            {"title": "AFR", "verses": [verse + "Translation detail." for verse in verses]},
        ]
        original = copy.deepcopy(columns)
        packed, ranges = paginate_bible_columns(columns, ("Segoe UI", 12, "normal"), 640, 360)
        self.assertTrue(any(stop - start > 1 for start, stop in ranges))
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], len(verses))
        for column, pages in zip(columns, packed):
            self.assertEqual(pages["verses"], ["\n\n".join(column["verses"][start:stop]) for start, stop in ranges])
            self.assertEqual([verse for page in pages["verses"] for verse in page.split("\n\n")], column["verses"])
        self.assertEqual(columns, original)
        oversized = "2. " + "Entire long verse. " * 100
        packed, ranges = paginate_bible_columns(
            [{"title": "KJV", "verses": ["1. Short verse.", oversized, "3. Next verse."]}],
            ("Segoe UI", 12, "normal"), 320, 180,
        )
        self.assertIn((1, 2), ranges)
        self.assertIn(oversized, packed[0]["verses"])
        self.assertEqual([verse for page in packed[0]["verses"] for verse in page.split("\n\n")],
                         ["1. Short verse.", oversized, "3. Next verse."])

    def test_bible_pagination_keeps_later_pages_grouped_and_readable(self):
        """What: Keep varied whole verses grouped at a stable readable display size.

        Why included: Genesis-like chapters previously grouped only early verses
        and translation titles forced the scripture text into a tiny font.
        Where used: Bible paging and overlay regression discovery.
        Returns: None; checks all verse boundaries, heading space, and rendered fonts.
        """
        import overlay_layout as layout

        lengths = [52, 115, 53, 95, 116, 115, 139, 88, 126, 123, 176,
                   182, 47, 197, 104, 136, 83, 129, 48, 147, 203, 133,
                   47, 166, 193, 218, 115, 217, 176, 186, 109]
        verses = [f"{number}. " + "Creation text. " * max(1, length // 15)
                  for number, length in enumerate(lengths, 1)]
        columns = [
            {"title": "Afrikaans 1933-1953 (BSSA Mobile)", "verses": verses},
            {"title": "King James Version (KJV)", "verses": list(verses)},
        ]
        font_value = ("Arial", 8, "bold")
        original_font = layout.get_bible_locked_font_value(columns, font_value, 1920, 1080)
        self.assertGreaterEqual(original_font[1], 48)
        for header in ("", "Creation"):
            with self.subTest(header=header):
                columns[0]["theme_header"] = header
                packed, ranges = layout.paginate_bible_columns(columns, font_value, 1920, 1080)
                locked_font = layout.get_bible_locked_font_value(packed, font_value, 1920, 1080)
                self.assertEqual(locked_font, original_font)
                self.assertTrue(any(start >= 11 and stop - start > 1 for start, stop in ranges))
                for column, pages in zip(columns, packed):
                    self.assertEqual("\n\n".join(pages["verses"]), "\n\n".join(column["verses"]))
                with patch.object(layout, "render_overlay_image", wraps=layout.render_overlay_image) as render:
                    image = layout.render_bible_columns_overlay(
                        1920, 1080, packed, 0, font_value, locked_font_value=locked_font,
                    )
                self.assertIsNotNone(image.getbbox())
                body_calls = [call for call in render.call_args_list if call.args[1] > 300]
                self.assertEqual(len(body_calls), 2)
                self.assertTrue(all(call.args[2].font_value == locked_font for call in body_calls))
                self.assertTrue(all(call.args[2].font_value[1] < locked_font[1]
                                    for call in render.call_args_list if call.args[1] <= 300))

    def test_bible_pagination_ignores_multiline_bearing_overhang(self):
        """What: Use wrapped page height rather than aggregate glyph bearings.

        Why included: Multiline bounds can exceed the width even when every
        wrapped line fits, prematurely forcing later verses onto separate pages.
        Where used: Bible pagination regression discovery.
        Returns: None; verifies that whole-verse groups continue through a chapter.
        """
        import overlay_layout as layout

        columns = [{"title": "KJV", "verses": [f"{number}. Complete verse." for number in range(1, 32)]}]
        with patch.object(layout, "wrap_text_to_width", side_effect=lambda text, *_args: text), \
                patch.object(layout, "get_text_bbox", side_effect=lambda text, *_args: (
                    -2, 0, 10000, (text.count("\n\n") + 1) * 300,
                )):
            _packed, ranges = layout.paginate_bible_columns(columns, ("Arial", 8, "bold"), 1920, 1080)
        self.assertEqual(ranges, [(start, min(start + 2, 31)) for start in range(0, 31, 2)])

    def test_bible_overlay_font_and_pixel_fitting(self):
        """What: Check independent verse fitting and stable rendered selection.

        Why included: Translation headings must not limit the shared verse font.
        Where used: Unittest-based Bible overlay regression discovery.
        Returns: None; invokes pixel fitting, verse selection, and header checks.
        """
        test_bible_locked_font_matches_original_pixel_fitting()
        test_locked_bible_overlay_preserves_verse_and_font()
        test_bible_theme_header_is_shared_across_columns()

    def test_preparation_display_and_print_use_preaching_label(self):
        """What: Label Preparation as Preaching in Display and both print modes.

        Why included: Service lists must identify the sermon without changing saved headings.
        Where used: Display/print regression discovery.
        Returns: None; checks labels, preserved content, scope, and unchanged song titles.
        """
        import ast
        import copy
        import tab_display
        from printer import build_print_sections_from_queue, build_song_list_sections_from_queue

        parsed = ast.parse(Path(tab_display.__file__).read_text())
        formatter = next(node for node in ast.walk(parsed) if isinstance(node, ast.FunctionDef)
                         and node.name == "format_queue_source_indicator")
        namespace = {}
        exec(compile(ast.Module(body=[formatter], type_ignores=[]), tab_display.__file__, "exec"), namespace)
        items = [
            {"id": "prep", "source": "preparation", "title": "Preparation", "text": "Sermon body"},
            {"id": "custom", "source": "preparation", "title": "Faith", "text": "Custom sermon"},
            {"id": "song", "source": "library", "title": "Amazing Grace", "subtitle": "Song [G]", "verses": ["Verse one"]},
        ]
        original = copy.deepcopy(items)
        self.assertEqual(namespace[formatter.name](items[0]), "Preaching")
        self.assertEqual(namespace[formatter.name](items[2]), "Song")
        sections = build_print_sections_from_queue(items, scope="all")
        self.assertEqual([section["title"] for section in sections], ["Preaching", "Preaching: Faith", "Amazing Grace [G]"])
        self.assertEqual(sections[0]["body"], "Sermon body")
        selected = build_print_sections_from_queue(items, selected_item_id="custom")
        self.assertEqual([section["title"] for section in selected], ["Preaching: Faith"])
        listing = build_song_list_sections_from_queue(items, scope="all")
        self.assertEqual(listing[0]["body"], "1. Preaching\n2. Preaching: Faith\n3. Amazing Grace [G]")
        self.assertEqual(items, original)


def test_mirror_portrait_background_fits_and_landscape_still_crops():
    """What: Fit portrait and EXIF-rotated images in mirror and OBS, retaining landscape cover.

    Why included: Portrait artwork must remain fully visible on widescreen displays.
    Where used: Mirror background rendering regression checks.
    Returns: None; compares actual preview pixels to Pillow's expected sizing.
    """
    from PIL import Image, ImageOps
    from types import SimpleNamespace
    import obs_secondary_display as secondary

    root = tk.Tk()
    root.withdraw()
    controller = MirrorDisplayController(
        root, get_app_base_path=lambda: Path("."), get_display_config=lambda: {},
        get_preview_size=lambda: (320, 180), resolve_background_path=lambda path: path,
        serialize_overlay_font=lambda font: None, build_hot_background_paths=lambda paths: [],
        font_value_factory=lambda: ("Segoe UI", 18, "normal"),
    )
    secondary_controller = SimpleNamespace(
        get_display_config=lambda: {"display_bounds": {"width": 320, "height": 180}},
        current_window_mode="projector", resolve_background_path=lambda path: path,
        current_overlay_source="preparation", current_overlay_text="",
        current_overlay_font=("Segoe UI", 18, "normal"), current_overlay_scroll_enabled=False,
        current_overlay_sections=None, current_overlay_columns=[], current_selected_verse_index=0,
        current_overlay_corner_text="", serialize_overlay_font=lambda font: None,
        current_overlay_foreground=None, current_overlay_scroll_speed=7,
        current_overlay_scroll_origin=0, current_transition_revision=0,
        current_hot_background_paths=[],
    )
    try:
        with tempfile.TemporaryDirectory() as directory:
            for name, dimensions, orientation in [("portrait.jpg", (40, 100), None), ("rotated.jpg", (100, 40), 6), ("landscape.jpg", (120, 60), None)]:
                path = Path(directory) / name
                image = Image.new("RGB", dimensions, "red")
                exif = Image.Exif()
                if orientation:
                    exif[274] = orientation
                image.save(path, exif=exif)
                with Image.open(path) as source:
                    oriented = ImageOps.exif_transpose(source).convert("RGB")
                    fit = ImageOps.pad if oriented.width < oriented.height else ImageOps.fit
                    expected = fit(oriented, (320, 180), method=Image.Resampling.LANCZOS)
                actual = controller.get_cached_preview_background(path, 320, 180)
                assert actual.tobytes() == expected.tobytes()
                assert (actual.getpixel((0, 90)) == (0, 0, 0)) == (oriented.width < oriented.height)
                secondary_controller.current_background_path = path
                with patch.object(secondary, "fit_overlay_layout", wraps=secondary.fit_overlay_layout) as fit_layout:
                    secondary.ObsSecondaryDisplayController._build_state(secondary_controller)
                secondary_background = fit_layout.call_args.kwargs["base_image"]
                assert secondary_background.tobytes() == expected.tobytes()
    finally:
        controller.close()
        root.destroy()


def test_preparation_preview_decodes_video_and_preserves_images():
    """What: Decode actual Preparation media through its loader and animation callbacks.

    Why included: Preparation previously treated MP4 as a still image and cropped portraits.
    Where used: Preview regression checks with disposable images/videos and no live data.
    Returns: None; checks frames, looping, image fitting, GIF motion, and cleanup.
    """
    import ast
    from types import SimpleNamespace
    import imageio.v2 as imageio
    import numpy as np
    from PIL import Image
    import tab_preparation as preparation
    from overlay_layout import fit_background_image

    parsed = ast.parse(Path(preparation.__file__).read_text())
    build = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == "build_preparation_tab")
    names = {"load_background_base_image", "close_preview_video", "schedule_preview_video_animation",
             "cancel_preview_gif_animation", "clear_preview_gif_cache", "ensure_preview_gif_cache", "schedule_preview_gif_animation",
             "build_full_preview_text", "compose_display_block"}
    functions = [node for node in build.body if isinstance(node, ast.FunctionDef) and node.name in names]
    clock = [0.0]
    active = [True]
    label = Mock(spec=tk.Label)
    label.after.return_value = "media-timer"
    header = Mock()
    header.get.return_value = ""
    body = Mock(spec=tk.Text)
    body.get.return_value = ""
    namespace = vars(preparation).copy()
    namespace.update(state={"header_var": header}, text_block=body, time=SimpleNamespace(monotonic=lambda: clock[0]), preview_label=label,
                     resolve_preparation_background_path=lambda path: Path(path),
                     is_preparation_tab_active=lambda: active[0], render_selected_block_preview=Mock())
    exec(compile(ast.Module(body=functions, type_ignores=[]), preparation.__file__, "exec"), namespace)
    state = namespace["state"]
    assert namespace["build_full_preview_text"]() == ""
    assert namespace["compose_display_block"]("", "") == ""
    assert namespace["compose_display_block"]("Entered heading", "Entered body") == "Entered heading\n\nEntered body"
    try:
        with tempfile.TemporaryDirectory(prefix="LightPreviewMedia-") as directory:
            root = Path(directory)
            frames = [np.full((48, 64, 3), color, dtype=np.uint8) for color in ((230, 20, 20), (20, 20, 230))]
            video = root / "preview.mp4"
            imageio.mimwrite(video, frames, fps=2)
            state["background_path"] = str(video)
            first = namespace["load_background_base_image"](320, 180)
            clock[0] = 0.6
            second = namespace["load_background_base_image"](320, 180)
            assert first.getpixel((160, 90)) != second.getpixel((160, 90))
            clock[0] = 10.0
            assert namespace["load_background_base_image"](320, 180).tobytes() == first.tobytes()
            assert state["preview_video_after_id"] == "media-timer"
            active[0] = False
            label.after.call_args.args[1]()
            assert state["preview_video_reader"] is None and state["preview_video_after_id"] is None
            active[0] = True
            for suffix in (".jpg", ".png", ".bmp", ".webp", ".tiff"):
                image_path = root / ("portrait" + suffix)
                Image.new("RGB", (40, 100), "red").save(image_path)
                state["background_path"] = str(image_path)
                actual = namespace["load_background_base_image"](320, 180)
                with Image.open(image_path) as source:
                    expected = fit_background_image(source, (320, 180))
                assert actual.tobytes() == expected.tobytes(), suffix
            gif = root / "portrait.gif"
            gif_frames = [Image.new("RGB", (40, 100), color) for color in ("red", "blue")]
            gif_frames[0].save(gif, save_all=True, append_images=gif_frames[1:], duration=100, loop=0)
            state["background_path"] = str(gif)
            first_gif = namespace["load_background_base_image"](320, 180)
            label.after.call_args.args[1]()
            second_gif = namespace["load_background_base_image"](320, 180)
            assert first_gif.getpixel((160, 90)) != second_gif.getpixel((160, 90))
            assert first_gif.getpixel((0, 90)) == (0, 0, 0)
            namespace["clear_preview_gif_cache"]()
    finally:
        namespace["clear_preview_gif_cache"]()
        namespace["close_preview_video"]()


def test_secondary_image_fit_tracks_portrait_and_landscape():
    """What: Run the OBS browser's image activation for portrait and landscape sizes.

    Why included: Image changes must reset fitting without affecting video cover mode.
    Where used: Secondary-screen browser source regression checks.
    Returns: None; asserts portrait contain and landscape cover using Node.js.
    """
    from obs_secondary_display import SECONDARY_DISPLAY_HTML

    node = shutil.which("node")
    if node is None:
        raise unittest.SkipTest("Node.js is required for the browser image fit regression")
    show_image = SECONDARY_DISPLAY_HTML.split("function showImage(path, state) {", 1)[1].split("\nfunction ", 1)[0]
    program = """
const assert = require('node:assert/strict');
let dimensions = [719, 1045];
const primaryImageLayer = {style: {}, classList: {add() {}, remove() {}}, removeAttribute() {}, complete: true,
    get naturalWidth() { return dimensions[0]; }, get naturalHeight() { return dimensions[1]; }};
const imageLayers = [primaryImageLayer, {classList: {remove() {}}, removeAttribute() {}}];
const videoLayers = [];
let activeImageIndex, activeKind, currentBackgroundPath, currentMediaRevision;
function mediaUrl(path) { return path; }
function resetLayer() {} function applyText() {} function clearMedia() {}
class Image { set src(value) { this.onload(); } }
function showImage(path, state) {
""" + show_image + """
showImage('portrait.jpg', {}); assert.equal(primaryImageLayer.style.objectFit, 'contain');
dimensions = [1920, 1080]; showImage('landscape.jpg', {}); assert.equal(primaryImageLayer.style.objectFit, 'cover');
"""
    result = subprocess.run([node, "-e", program], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_mirror_video_frames_advance_loop_and_release_reader():
    """What: Decode changing local video pixels, restart at EOF, and release the decoder.

    Why included: The mirror previously substituted a black image for every MP4 background.
    Where used: Local video preview regression checks without OBS or live application data.
    Returns: None; asserts frame indices, output pixels, timer cancellation, and cleanup.
    """
    import numpy as np
    import mirror
    from PIL import Image

    root = tk.Tk()
    root.withdraw()
    controller = MirrorDisplayController(
        root, get_app_base_path=lambda: Path("."), get_display_config=lambda: {},
        get_preview_size=lambda: (320, 180), resolve_background_path=lambda path: path,
        serialize_overlay_font=lambda font: None, build_hot_background_paths=lambda paths: [],
        font_value_factory=lambda: ("Segoe UI", 18, "normal"),
    )
    red = np.full((16, 16, 3), (255, 0, 0), dtype=np.uint8)
    green = np.full((16, 16, 3), (0, 255, 0), dtype=np.uint8)
    opened = []

    class FakeReader:
        """Two-frame decoder that raises at end-of-file like imageio's ffmpeg reader."""

        def __init__(self):
            """What: Start a fake decoder with no frames requested yet.

            Why included: Tests need to inspect which frames were read and whether the decoder was closed.
            Where used: fake_get_reader() in the mirror video loop test.
            Returns: None.
            """
            self.calls = []
            self.closed = False

        def get_meta_data(self):
            """What: Report the fake clip's frame rate.

            Why included: The looping reader reads fps from metadata when it opens a clip.
            Where used: LoopingVideoReader during the mirror video loop test.
            Returns: Metadata mapping with fps.
            """
            return {"fps": 24}

        def get_data(self, index):
            """What: Return a fixture frame, or fail at the clip end like imageio's ffmpeg reader.

            Why included: The test proves the preview restarts the loop when a clip ends.
            Where used: LoopingVideoReader during the mirror video loop test.
            Returns: Frame array; raises IndexError past the last frame.
            """
            self.calls.append(index)
            if index >= 2:
                raise IndexError("EOF")
            return (red, green)[index]

        def close(self):
            """What: Mark the fake decoder as closed.

            Why included: The test asserts decoders are released after a loop and when the background changes.
            Where used: LoopingVideoReader during the mirror video loop test.
            Returns: None.
            """
            self.closed = True

    def fake_get_reader(path, **_kwargs):
        """What: Create and record a fake decoder in place of imageio.get_reader.

        Why included: The test counts how many decoders the looping reader opens.
        Where used: Patched video_loop.imageio.get_reader in the mirror video loop test.
        Returns: A new FakeReader.
        """
        reader = FakeReader()
        opened.append(reader)
        return reader

    clock = [100.0]
    controller.preview_label = tk.Label(root)
    controller.background_path = "fixture.mp4"
    try:
        with patch("video_loop.imageio.get_reader", side_effect=fake_get_reader), \
                patch("mirror.time.monotonic", side_effect=lambda: clock[0]):
            first = controller.get_cached_preview_background(Path("fixture.mp4"), 80, 45)
            clock[0] = 100.042
            second = controller.get_cached_preview_background(Path("fixture.mp4"), 80, 45)
            clock[0] = 100.1
            looped = controller.get_cached_preview_background(Path("fixture.mp4"), 80, 45)
        assert first.getpixel((0, 0)) == (255, 0, 0)
        assert second.getpixel((0, 0)) == (0, 255, 0)
        assert looped.getpixel((0, 0)) == (255, 0, 0)
        assert len(opened) == 2, "End of clip must restart on a fresh decoder"
        assert opened[0].calls == [0, 0, 1, 2]
        assert opened[1].calls == [0, 0]
        controller.sync_preview_animations()
        assert controller.preview_video_after_id
        with tempfile.TemporaryDirectory() as directory:
            static = Path(directory) / "static.png"
            Image.new("RGB", (80, 45), "blue").save(static)
            controller.background_path = str(static)
            controller.get_cached_preview_background(static, 80, 45)
        controller.sync_preview_animations()
        assert controller.preview_video_after_id is None
        assert controller.preview_video_reader is None
        assert opened[0].closed, "Exhausted decoder must be retired after the loop"
        assert opened[1].closed, "Active decoder must close when the background changes"
    finally:
        controller.close()
        root.destroy()


def test_welcome_video_preview_animates_and_closes_on_destroy():
    """What: Restore an MP4 background into real Welcome widgets and advance its timer.

    Why included: Welcome previously accepted videos in its picker but rendered black pixels.
    Where used: Welcome local video/overlay and decoder cleanup regression checks.
    Returns: None; asserts changing frame pixels and reader closure without external output.
    """
    import numpy as np
    import tab_welcome
    from PIL import ImageTk

    root = tk.Tk()
    root.withdraw()
    welcome_state = {}
    colors = [np.full((16, 16, 3), color, dtype=np.uint8) for color in [(255, 0, 0), (0, 255, 0)]]
    opened = []

    class FakeReader:
        """Two-frame decoder that serves frames by index like imageio's ffmpeg reader."""

        def __init__(self):
            """What: Start a fake decoder with no frames requested yet.

            Why included: Tests need to inspect which frames were read and whether the decoder was closed.
            Where used: fake_get_reader() in the Welcome video test.
            Returns: None.
            """
            self.calls = []
            self.closed = False

        def get_meta_data(self):
            """What: Report the fake clip's frame rate.

            Why included: The looping reader reads fps from metadata when it opens a clip.
            Where used: LoopingVideoReader during the Welcome video test.
            Returns: Metadata mapping with fps.
            """
            return {"fps": 24}

        def get_data(self, index):
            """What: Return a fixture frame, or fail at the clip end like imageio's ffmpeg reader.

            Why included: The Welcome preview must show changing frames from the fake clip.
            Where used: LoopingVideoReader during the Welcome video test.
            Returns: Frame array; raises IndexError past the last frame.
            """
            self.calls.append(index)
            if index >= len(colors):
                raise IndexError("EOF")
            return colors[index]

        def close(self):
            """What: Mark the fake decoder as closed.

            Why included: The test asserts the Welcome tab closes its decoder when destroyed.
            Where used: LoopingVideoReader during the Welcome video test.
            Returns: None.
            """
            self.closed = True

    def fake_get_reader(path, **_kwargs):
        """What: Create and record a fake decoder in place of imageio.get_reader.

        Why included: The test checks that exactly one decoder is opened and later closed.
        Where used: Patched video_loop.imageio.get_reader in the Welcome video test.
        Returns: A new FakeReader.
        """
        reader = FakeReader()
        opened.append(reader)
        return reader

    clock = [100.0]
    callbacks = []
    original_after = tk.Label.after

    def placeholder(parent, title):
        """What: Build isolated preview widgets. Why included: Avoid full app startup.

        Where used: Welcome test context. Returns: Frame and image label.
        """
        frame = tk.Frame(parent, width=320, height=180)
        label = tk.Label(frame, text=title)
        label.pack()
        return frame, label

    def after(label, delay, callback=None, *args):
        """What: Capture video timers. Why included: Avoid timing-dependent event-loop waits.

        Where used: Patched Tk label scheduler. Returns: Timer ID or normal Tk after result.
        """
        if delay == 67:
            callbacks.append(callback)
            return "video-test-timer"
        return original_after(label, delay, callback, *args)

    try:
        with patch("tab_welcome.load_welcome_notes", return_value=[]), \
                patch("tab_welcome.get_upcoming_birthdays", return_value=[]), \
                patch("tab_welcome.resolve_welcome_background_path", side_effect=lambda value: Path(value) if value else None), \
                patch("video_loop.imageio.get_reader", side_effect=fake_get_reader), \
                patch("tab_welcome.time.monotonic", side_effect=lambda: clock[0]), \
                patch("tab_welcome.start_async_preview_render", side_effect=lambda **kwargs: kwargs["apply_result"](kwargs["render_worker"]())), \
                patch("tab_welcome.ImageTk.PhotoImage", wraps=ImageTk.PhotoImage) as photos, \
                patch.object(tk.Label, "after", after):
            tab_welcome.build_welcome_tab(tk.Frame(root), {
                "tab_widgets": [], "tab_labels": [], "tab_inputs": [],
                "build_preview_placeholder": placeholder, "apply_layout_style": lambda *args, **kwargs: None,
                "welcome_state": welcome_state, "get_preview_size": lambda: (320, 180),
                "get_display_bounds": lambda: {"width": 640, "height": 360},
            })
            welcome_state["restore_service_entry"]({"queue_items": [{"source": "welcome", "text": "Welcome", "background_path": "fixture.mp4"}]})
            welcome_state["refresh_preview"]()
            assert photos.call_args.args[0].getpixel((0, 0)) == (255, 0, 0), (photos.call_args.args[0].getpixel((0, 0)), [reader.calls for reader in opened], photos.call_count)
            clock[0] = 100.05
            callbacks.pop(0)()
            assert photos.call_args.args[0].getpixel((0, 0)) == (0, 255, 0)
            root.destroy()
            assert len(opened) == 1 and opened[0].closed, "Welcome must close its decoder on destroy"
    finally:
        if root.tk.call("info", "commands", "."):
            root.destroy()


def test_tab_headers_follow_every_theme():
    """What: Check selected, idle, and hovered tab text under every palette.

    Why included: Native Windows tabs previously retained black labels on dark themes.
    Where used: Main theme-selection regression checks.
    Returns: None; verifies a colorable renderer and all configured foreground states.
    """
    import main

    root = tk.Tk()
    root.withdraw()
    try:
        notebook = ttk.Notebook(root, style="ContentTabs.TNotebook")
        style = ttk.Style(root)
        for palette in main.THEMES.values():
            main.configure_tabs_notebook_style(notebook, palette, "Segoe UI", "Bold", 12)
            assert style.theme_use() == "clam"
            for states, color in [(('!selected', '!active'), palette["tabs_fg"]), (('selected',), palette["menu_fg"]), (('active', '!selected'), palette["text"])]:
                assert style.lookup("ContentTabs.TNotebook.Tab", "foreground", states) == color
    finally:
        root.destroy()


def test_welcome_notes_save_and_recall_independent_backgrounds():
    """What: Save two real Welcome templates with different backgrounds and reselect them.

    Why included: Each note must retain its own media instead of inheriting another selection.
    Where used: Welcome picker, SQLite settings persistence, and note-list regression checks.
    Returns: None; checks persisted paths and recalled editor background settings.
    """
    import main
    import tab_welcome

    root = tk.Tk()
    root.withdraw()
    widgets = []
    picked = {"path": "Backgrounds/Images/first.png"}
    try:
        with tempfile.TemporaryDirectory() as directory, \
                patch("tab_welcome.get_welcome_note_db_path", return_value=Path(directory) / "notes.db"), \
                patch("tab_welcome.get_upcoming_birthdays", return_value=[]), \
                patch("tab_welcome.resolve_welcome_background_path", return_value=None), \
                patch("tab_welcome.open_background_picker", side_effect=lambda **kwargs: kwargs["on_apply"](picked["path"])):
            tab_welcome.build_welcome_tab(tk.Frame(root), {
                "tab_widgets": widgets, "tab_labels": [], "tab_inputs": [],
                "build_preview_placeholder": main.build_preview_placeholder,
                "apply_layout_style": lambda *args, **kwargs: None, "welcome_state": {},
            })
            notes = next(widget for widget in widgets if isinstance(widget, tk.Listbox))
            title = notes.master.grid_slaves(row=1, column=0)[0]
            editor = next(widget for widget in widgets if isinstance(widget, tk.Text))
            buttons = {widget.cget("text"): widget for widget in widgets if isinstance(widget, tk.Button)}
            for name, path in [("First", "Backgrounds/Images/first.png"), ("Second", "Backgrounds/Images/second.png")]:
                buttons["New"].invoke()
                title.insert(0, name)
                editor.insert("1.0", name + " welcome")
                picked["path"] = path
                buttons["Background"].invoke()
                buttons["Save"].invoke()
            saved = tab_welcome.load_welcome_notes()
            assert [json.loads(note["settings_json"])["background_path"] for note in saved] == ["Backgrounds/Images/first.png", "Backgrounds/Images/second.png"]
            assert not notes.cget("exportselection")
            notes.master.master.pack(fill="both", expand=True)
            root.deiconify()
            root.update()
            for index, note in enumerate(saved):
                notes.selection_clear(0, tk.END)
                notes.selection_set(index)
                notes.event_generate("<<ListboxSelect>>", when="now")
                path = json.loads(note["settings_json"])["background_path"]
                assert title.get() == note["title"]
                buttons["Save"].invoke()
                recalled = next(saved_note for saved_note in tab_welcome.load_welcome_notes() if saved_note["id"] == note["id"])
                assert json.loads(recalled["settings_json"])["background_path"] == path
    finally:
        root.destroy()


def test_library_show_live_activates_selected_song_in_both_outputs():
    """What: Show a selected Library verse directly through the real mirror controller.

    Why included: Show Live must not alter the planned Display schedule or selection.
    Where used: Library button and shared mirror/secondary handoff regression.
    Returns: None; verifies live text/media and an unchanged schedule file.
    """
    import main
    import display_bridge
    import tab_library

    root = tk.Tk()
    root.withdraw()
    widgets = []
    display_state = {"refresh_from_bridge": Mock(), "set_queue_item_active": Mock()}
    controller = MirrorDisplayController(
        root, get_app_base_path=lambda: Path("."), get_display_config=lambda: {},
        get_preview_size=lambda: (320, 180), resolve_background_path=lambda path: path,
        serialize_overlay_font=lambda font: None, build_hot_background_paths=lambda paths: [],
        font_value_factory=lambda: ("Segoe UI", 18, "normal"),
        get_music_licenses=lambda: {"ccli": "12345", "onelicense": "OL-88"},
    )
    controller.enabled_var.set(True)
    song = {"id": 7, "title": "Fixture", "song_key": "C", "verses": ["First verse", "Second verse"], "background_path": "Backgrounds/Images/song.png", "license_provider": "CCLI", "author": "Ada Lovelace"}
    try:
        with tempfile.TemporaryDirectory() as directory, \
                patch("display_bridge.get_display_queue_bridge_path", return_value=Path(directory) / "queue.json"), \
                patch.object(controller, "request_push_to_secondary") as secondary_push, \
                patch("tab_library.upsert_display_queue_item", wraps=display_bridge.upsert_display_queue_item) as queue_write, \
                patch("tab_library.messagebox.showinfo"), \
                patch("tab_library.load_all_songs", return_value=[song]):
            display_bridge.save_display_queue_state({"queue_items": [dict(song, id="existing-song", source="library", source_item_id="7", text="First verse", selected_verse_index=0, active=True)]})
            before = (Path(directory) / "queue.json").read_bytes()
            tab_library.build_library_tab(tk.Frame(root), {
                "tab_widgets": widgets, "tab_labels": [], "tab_inputs": [],
                "display_state": display_state, "apply_layout_style": lambda *args, **kwargs: None,
                "handle_display_live_change": controller.set_live_payload,
                "get_music_licenses": lambda: {"ccli": "12345", "onelicense": "OL-88"},
                "build_preview_placeholder": main.build_preview_placeholder,
                "get_display_bounds": lambda: {"width": 640, "height": 360},
            })
            button = next(widget for widget in widgets if isinstance(widget, tk.Button) and widget.cget("text") == "Show Live")
            button_frame = button.master.master
            editors = []
            pending = [button_frame]
            while pending:
                widget = pending.pop()
                pending.extend(reversed(widget.winfo_children()))
                if isinstance(widget, tk.Text):
                    editors.append(widget)
            root.deiconify()
            button_frame.pack(fill="both", expand=True)
            root.update()
            editors[1].event_generate("<FocusIn>", when="now")
            assert button.cget("state") == "normal"
            license_picker = next(widget for widget in widgets if isinstance(widget, ttk.Combobox) and "OneLicense" in tuple(widget.cget("values")))
            assert tuple(license_picker.cget("values")) == ("Public Domain", "CCLI", "OneLicense", "Individual")
            author_entry = next(widget for widget in widgets if isinstance(widget, tk.Entry) and int(widget.grid_info().get("row", -1)) == 1 and int(widget.grid_info().get("column", -1)) == 3)
            assert author_entry.get() == "Ada Lovelace"
            button.invoke()
            assert controller.preview_text == "Second verse"
            assert controller.preview_key == "C"
            assert controller.get_live_corner_text() == "C\nFixture\nAuthor: Ada Lovelace\nCCLI License: 12345"
            assert controller.show_song_metadata
            assert controller.song_metadata_after_id is not None
            controller.hide_song_metadata()
            assert controller.get_live_corner_text() == "C"
            assert not controller.show_song_metadata
            assert Path(controller.background_path) == Path(song["background_path"])
            assert controller.selected_verse_index == 1
            assert secondary_push.call_count == 2
            assert (Path(directory) / "queue.json").read_bytes() == before
            queue_write.assert_not_called()
            for callback in display_state.values():
                callback.assert_not_called()
            send_button = next(widget for widget in widgets if isinstance(widget, tk.Button) and widget.cget("text") == "Send to Display")
            send_button.invoke()
            queue_write.assert_called_once()
            saved_item = next(item for item in display_bridge.load_display_queue_state()["queue_items"] if item.get("source") == "library")
            assert saved_item["license_provider"] == "CCLI"
            assert saved_item["license_author"] == "Ada Lovelace"
            assert secondary_push.call_count == 2
    finally:
        controller.close()
        root.destroy()


def test_native_background_picker_browses_without_prescanned_choices():
    """What: Check consistent native browsing, selected paths, cancellation, and validation.

    Why included: Empty media scans previously blocked background selection in Preparation.
    Where used: Shared background dialog regression checks for all four content tabs.
    Returns: None; verifies identical filters, existing source folders, and owner callbacks.
    """
    import background_picker
    from PIL import Image

    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        backgrounds = base / "Backgrounds"
        backgrounds.mkdir()
        external = base / "External Media"
        external.mkdir()
        selected = external / "background.png"
        Image.new("RGB", (16, 16), "green").save(selected)
        filters = []
        with patch("background_picker.get_app_base_path", return_value=base), \
                patch("background_picker.get_app_data_path", return_value=base / "Data"), \
                patch("background_picker.filedialog.askopenfilename", return_value=str(selected)) as dialog, \
                patch("background_picker.messagebox.showerror") as error:
            apply = Mock()
            for title in ["Select Preparation Background", "Select Welcome Background", "Select Background", "Select Bible Background"]:
                background_picker.open_background_picker(
                    parent_window=None, dialog_title=title, empty_message="No backgrounds", picker_heading="",
                    choices=[], current_background="", resolve_background_path=lambda value: Path(value),
                    video_extensions=set(), apply_layout_style=lambda *args, **kwargs: None, on_apply=apply,
                )
                assert Path(dialog.call_args.kwargs["initialdir"]) == base / "Data" / "Backgrounds"
                filters.append(dialog.call_args.kwargs["filetypes"])
            assert all(filetypes == filters[0] for filetypes in filters)
            assert "*.gif" in filters[0][0][1] and "*.mp4" in filters[0][0][1]
            assert apply.call_count == 4
            apply.assert_called_with(str(selected.resolve()))
            apply.reset_mock()
            arguments = dict(parent_window=None, dialog_title="Select Background", empty_message="", picker_heading="", choices=[], current_background=str(selected), resolve_background_path=lambda value: Path(value), video_extensions=set(), apply_layout_style=lambda *args, **kwargs: None, on_apply=apply)
            dialog.return_value = ""
            background_picker.open_background_picker(**arguments)
            assert Path(dialog.call_args.kwargs["initialdir"]) == external
            apply.assert_not_called()
            dialog.return_value = str(base / "missing.png")
            background_picker.open_background_picker(**arguments)
            error.assert_called_once()
            apply.assert_not_called()


def test_preparation_empty_text_requires_confirmation_and_preserves_blank_items():
    """What: Confirm or cancel sending an empty Preparation body, with and without a header.

    Why included: Operators must be able to schedule a header/background-only item intentionally.
    Where used: Preparation send dialog, queue normalization, and service recall regression checks.
    Returns: None; verifies cancellation, header preservation, blank persistence, and normal sends.
    """
    import main
    import tab_preparation
    import display_bridge

    root = tk.Tk()
    root.withdraw()
    widgets = []
    try:
        with tempfile.TemporaryDirectory() as directory, \
                patch("display_bridge.get_display_queue_bridge_path", return_value=Path(directory) / "queue.json"), \
                patch("tab_preparation.messagebox.askyesno", return_value=False) as confirm, \
                patch("tab_preparation.messagebox.showinfo"):
            display_bridge.save_display_queue_state({"queue_items": []})
            tab_preparation.build_preparation_tab(tk.Frame(root), {
                "tab_widgets": widgets, "tab_labels": [], "tab_inputs": [],
                "build_preview_placeholder": main.build_preview_placeholder,
                "apply_layout_style": lambda *args, **kwargs: None,
                "preparation_state": {}, "display_state": {},
            })
            send = next(widget for widget in widgets if isinstance(widget, tk.Button) and widget.cget("text") == "Send to Display")
            header = next(widget for widget in widgets if isinstance(widget, tk.Entry))
            editor = next(widget for widget in widgets if isinstance(widget, tk.Text) and widget.cget("state") != "disabled")
            send.invoke()
            confirm.assert_called_once()
            assert confirm.call_args.kwargs["default"] == "no"
            assert display_bridge.load_display_queue_state()["queue_items"] == []
            confirm.return_value = True
            send.invoke()
            assert display_bridge.load_display_queue_state()["queue_items"][0]["text"] == header.get()
            header.delete(0, tk.END)
            display_bridge.save_display_queue_state({"queue_items": []})
            send.invoke()
            items = display_bridge.load_display_queue_state()["queue_items"]
            assert len(items) == 1 and items[0]["text"] == "" and items[0]["verses"] == []
            assert items[0]["allow_empty_text"] is True
            restored = display_bridge._build_queue_items_from_service_entry({
                "id": 1, "display_tab_items": json.dumps(items), "welcome_settings": "{}",
                "presentation_settings": "{}", "saved_at": "", "welcome_field_one": "", "welcome_field_two": "",
            })
            assert restored[0]["text"] == "" and restored[0]["allow_empty_text"] is True
            assert display_bridge._normalize_queue_item({"source": "preparation", "title": "Legacy"})["text"] == "Legacy"
            confirm.reset_mock()
            editor.insert("1.0", "Normal preparation text")
            send.invoke()
            confirm.assert_not_called()
    finally:
        root.destroy()


def test_combined_bibles_keep_kjv_extra_books_and_named_display_payloads():
    """What: Select a shorter Bible before KJV and send additional books using real reader controls.

    Why included: Comparison must retain every selected source's books and show names instead of Book 67.
    Where used: Multi-Bible book union, legacy selection, and Display handoff regression checks.
    Returns: None; validates all 80 books, actual chapter keys, and named queue labels.
    """
    import tab_bible

    root = tk.Tk()
    root.withdraw()
    widgets = []
    refresh = Mock()
    sent = []

    def append_item(item):
        """What: Capture Bible queue items without changing the user's display bridge.

        Why included: The test needs the real sender's labels and columns, not persistent mutations.
        Where used: Patched Bible queue writer.
        Returns: Queue mapping containing the captured item.
        """
        sent.append(item)
        return {"queue_items": [item]}

    try:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            short = base / "short"
            kjv = base / "kjv"
            short.mkdir()
            kjv.mkdir()
            (base / "Data").mkdir()
            for number in range(1, 81):
                chapters = {"10": {"4": "Additional Esther verse", "6": "Another additional verse"}} if number == 69 else {"1": {"1": "KJV fixture verse"}}
                (kjv / f"book_{number:02d}.json").write_text(json.dumps({"book_number": number, "chapters": chapters}), encoding="utf-8")
                if number <= 66:
                    (short / f"book_{number:02d}.json").write_text(json.dumps({"book_number": number, "chapters": {"1": {"1": "Short Bible verse"}}}), encoding="utf-8")
            (base / "Data" / "bible_settings.json").write_text(json.dumps({
                "selected_bibles": ["Short Bible", "KJV"], "book": "67 Book 67",
                "chapter": "1", "start_verse": "1", "end_verse": "1",
            }), encoding="utf-8")
            sources = [{"id": "short", "display_name": "Short Bible", "cache_directory": short},
                       {"id": "kjv", "display_name": "KJV", "cache_directory": kjv}]
            with patch("tab_bible.discover_cached_bible_sources", return_value=sources), \
                    patch("tab_bible.append_display_queue_item", side_effect=append_item):
                tab_bible.build_bible_reader_tab(tk.Frame(root), {
                    "tab_widgets": widgets, "tab_labels": [], "tab_inputs": [],
                    "apply_layout_style": lambda *args, **kwargs: None,
                    "get_app_base_path": lambda: base,
                    "display_state": {"refresh_from_bridge": refresh},
                })
                book, chapter, first, last = [widget for widget in widgets if isinstance(widget, ttk.Combobox)]
                assert len(book["values"]) == 80
                assert book.get() == "67 Tobit"
                assert "80 2 Esdras" in book["values"]
                send = next(widget for widget in widgets if isinstance(widget, tk.Button) and widget.cget("text") == "Send to Display")
                send.invoke()
                assert sent[-1]["title"] == "Tobit 1:1-1"
                assert sent[-1]["verse_labels"] == ["Tobit 1:1"]
                assert sent[-1]["display_columns"][0]["verses"] == ["1. [Verse not available]"]
                assert sent[-1]["display_columns"][1]["verses"] == ["1. KJV fixture verse"]
                book.set("69 Additions to Esther")
                book.event_generate("<<ComboboxSelected>>")
                assert tuple(chapter["values"]) == ("10",)
                assert tuple(first["values"]) == ("4", "6")
                assert first.get() == "4"
                assert last.get() == "6"
                send.invoke()
                assert sent[-1]["title"] == "Additions to Esther 10:4-6"
                assert sent[-1]["verse_labels"][0] == "Additions to Esther 10:4"
                book.set("01 Genesis")
                book.event_generate("<<ComboboxSelected>>")
                send.invoke()
                assert sent[-1]["title"] == "Genesis 1:1-1"
                assert sent[-1]["display_columns"][0]["verses"] == ["1. Short Bible verse"]
                assert sent[-1]["display_columns"][1]["verses"] == ["1. KJV fixture verse"]
                assert refresh.call_count == 3
    finally:
        root.destroy()


def test_library_queue_updates_preserve_position_identity_and_active_state():
    """What: Replace an edited/renamed song in place using legacy and current Library identity.

    Why included: Re-sending must retain order and live state without replacing another same-title song.
    Where used: Shared display bridge Library update regressions.
    Returns: None; uses temporary queue storage and checks both updates and first additions.
    """
    import display_bridge

    with tempfile.TemporaryDirectory() as directory, \
            patch("display_bridge.get_display_queue_bridge_path", return_value=Path(directory) / "queue.json"):
        display_bridge.save_display_queue_state({"queue_items": [
            {"id": "welcome-row", "source": "welcome", "title": "Welcome"},
            {"id": "library-7-0-original", "source": "library", "title": "Old Title", "active": True,
             "verses": ["Old verse one", "Old verse two"], "selected_verse_index": 1},
            {"id": "other-song", "source": "library", "source_item_id": "8", "title": "New Title", "verses": ["Other song"]},
        ]})
        updated = {"id": "library-7-0-new", "source": "library", "source_item_id": "7", "title": "New Title",
                   "verses": ["Updated verse one", "Updated verse two"], "text": "Updated verse one", "background_path": "updated.png"}
        result = display_bridge.upsert_display_queue_item(updated, match_title=True)
        assert [item["id"] for item in result["queue_items"]] == ["welcome-row", "library-7-0-original", "other-song"]
        song = result["queue_items"][1]
        assert song["title"] == "New Title"
        assert song["active"] is True
        assert song["selected_verse_index"] == 1
        assert song["source_item_id"] == "7"
        assert song["background_path"] == "updated.png"
        assert result["queue_items"][2]["verses"] == ["Other song"]
        updated["id"] = "library-7-0-again"
        updated["verses"] = ["Only remaining verse"]
        result = display_bridge.upsert_display_queue_item(updated, match_title=True)
        assert result["queue_items"][1]["selected_verse_index"] == 0
        assert len(result["queue_items"]) == 3
        new_song = dict(updated, id="library-9-0-new", source_item_id="9")
        result = display_bridge.upsert_display_queue_item(new_song, match_title=True)
        assert len(result["queue_items"]) == 4
        assert result["queue_items"][-1]["id"] == "library-9-0-new"
        assert display_bridge.load_display_queue_state()["queue_items"][1]["source_item_id"] == "7"


def test_send_buttons_confirm_added_and_updated_content():
    """What: Send and resend content through Library, Welcome, and Preparation buttons.

    Why included: Successful updates must refresh Display and report updates instead of duplicate rejection.
    Where used: Source-tab send confirmation regression checks with temporary queue storage.
    Returns: None; verifies new lyrics, stable queue slots, and all six added/updated messages.
    """
    import display_bridge
    import tab_library
    import tab_welcome
    import tab_preparation
    from main import build_preview_placeholder

    root = tk.Tk()
    root.withdraw()
    refresh = Mock()

    def descendants(widget):
        """What: Enumerate nested widgets for fixture control lookup.

        Why included: Dynamically built editors are not all present in the initial widget registry.
        Where used: Finding source-tab text fields and send buttons.
        Returns: Recursive widget list.
        """
        result = []
        for child in widget.winfo_children():
            result.append(child)
            result.extend(descendants(child))
        return result

    def context():
        """What: Supply isolated preview, typography, and Display callbacks to each tab.

        Why included: The regression must not build the full app or open external output.
        Where used: Test tab construction.
        Returns: Shared test context mapping.
        """
        return {"tab_widgets": [], "tab_labels": [], "tab_inputs": [],
                "build_preview_placeholder": build_preview_placeholder, "apply_layout_style": lambda *args, **kwargs: None,
                "display_state": {"refresh_from_bridge": refresh}, "get_preview_size": lambda: (320, 180),
                "get_display_bounds": lambda: {"width": 640, "height": 360}}

    try:
        with tempfile.TemporaryDirectory() as directory, \
                patch("display_bridge.get_display_queue_bridge_path", return_value=Path(directory) / "queue.json"), \
                patch("tab_library.load_all_songs", return_value=[{"id": 7, "title": "Fixture Song", "song_key": "C", "verses": ["Original lyric"], "background_path": ""}]), \
                patch("tab_welcome.load_welcome_notes", return_value=[]), \
                patch("tab_welcome.get_upcoming_birthdays", return_value=[]), \
                patch("tab_library.messagebox.showinfo") as info:
            display_bridge.save_display_queue_state({"queue_items": []})
            library = tk.Frame(root)
            tab_library.build_library_tab(library, context())
            editor = next(widget for widget in descendants(library) if isinstance(widget, tk.Text))
            send = next(widget for widget in descendants(library) if isinstance(widget, tk.Button) and widget.cget("text") == "Send to Display")
            send.invoke()
            editor.delete("1.0", "end")
            editor.insert("1.0", "Changed lyric")
            send.invoke()
            assert display_bridge.load_display_queue_state()["queue_items"][0]["verses"] == ["Changed lyric"]

            welcome = tk.Frame(root)
            welcome_context = context()
            welcome_context["welcome_state"] = {"combine_text_var": tk.BooleanVar(root, value=False), "activate_scroll_var": tk.BooleanVar(root, value=False)}
            tab_welcome.build_welcome_tab(welcome, welcome_context)
            editor = next(widget for widget in descendants(welcome) if isinstance(widget, tk.Text))
            send = next(widget for widget in descendants(welcome) if isinstance(widget, tk.Button) and widget.cget("text") == "Send to Display")
            editor.insert("1.0", "Welcome fixture")
            send.invoke()
            editor.insert("end", " updated")
            send.invoke()

            preparation = tk.Frame(root)
            preparation_context = context()
            preparation_context["preparation_state"] = {}
            tab_preparation.build_preparation_tab(preparation, preparation_context)
            header = next(widget for widget in descendants(preparation) if isinstance(widget, tk.Entry))
            header.insert(0, "Preparation fixture")
            send = next(widget for widget in descendants(preparation) if isinstance(widget, tk.Button) and widget.cget("text") == "Send to Display")
            send.invoke()
            send.invoke()
            messages = [call.args[1] for call in info.call_args_list]
            assert messages == ["Song added to Display.", "Song updated in Display.",
                                "Welcome note added to Display.", "Welcome note updated in Display.",
                                "Preparation added to Display.", "Preparation updated in Display."], messages
            assert refresh.call_count == 6
            assert len(display_bridge.load_display_queue_state()["queue_items"]) == 3
    finally:
        root.destroy()


def test_display_content_requires_double_click_but_show_button_stays_single_click():
    """What: Exercise queue-title and verse-label mouse events with single and double clicks.

    Why included: Touchpad taps must not unintentionally activate rows or change the displayed verse.
    Where used: Display first/second panel interaction regression checks.
    Returns: None; asserts deliberate double-click content actions and explicit single-click buttons.
    """
    root = tk.Tk()
    root.geometry("1100x700")
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)
    frame = tk.Frame(notebook)
    notebook.add(frame, text="Display")
    widgets = []
    display_state = {}
    item = {"id": "double-click-item", "source": "library", "active": False, "title": "Double Click Song",
            "verses": ["First test verse", "Second test verse"], "selected_verse_index": 0}
    bridge = {"queue_items": [item]}

    def toggle_item(item_id):
        """What: Toggle the fixture row through the real asynchronous Display mutation path.

        Why included: Mouse actions must exercise controller callbacks rather than mocked widget bindings.
        Where used: Patched shared-queue activation writer.
        Returns: Updated fixture queue state.
        """
        item["active"] = not item["active"]
        return bridge

    def select_verse(item_id, verse_index):
        """What: Record selected verse state without touching the user's queue file.

        Why included: Verse clicks should use the normal worker/UI handoff in an isolated fixture.
        Where used: Patched shared-queue verse writer.
        Returns: Updated fixture queue state.
        """
        item["selected_verse_index"] = verse_index
        return bridge

    def pump_until(predicate):
        """What: Process Tk callbacks until the interaction reaches its expected state.

        Why included: Queue actions execute on workers and then update Tk asynchronously.
        Where used: Test setup and deliberate activation/verse assertions.
        Returns: None; raises on timeout.
        """
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            root.update()
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("Display interaction timed out")

    def click(widget, timestamp):
        """What: Generate one normal press/release pair with a deterministic event timestamp.

        Why included: Tk recognises double-clicks from successive presses, not synthetic Double modifiers.
        Where used: Queue titles and verse labels in this regression.
        Returns: None.
        """
        widget.event_generate("<ButtonPress-1>", x=8, y=8, time=timestamp)
        widget.event_generate("<ButtonRelease-1>", x=8, y=8, time=timestamp + 1)
        root.update()

    try:
        with patch("tab_display.load_display_queue_state", return_value=bridge), \
                patch("tab_display.list_saved_service_entries", return_value=[]), \
                patch("tab_display.toggle_display_queue_item_active", side_effect=toggle_item) as toggle, \
                patch("tab_display.set_display_queue_item_selected_verse", side_effect=select_verse) as selection:
            build_display_tab(frame, {"tab_widgets": widgets, "tab_labels": widgets, "tab_inputs": widgets,
                "apply_layout_style": lambda *args, **kwargs: None, "display_state": display_state,
                "notebook": notebook})
            display_state["refresh_from_bridge"]()
            pump_until(lambda: any(isinstance(widget, tk.Button) and "Double Click Song" in widget.cget("text") for widget in widgets))
            title = next(widget for widget in widgets if isinstance(widget, tk.Button) and "Double Click Song" in widget.cget("text"))
            click(title, 1000)
            assert toggle.call_count == 0
            assert not item["active"]
            click(title, 1100)
            pump_until(lambda: toggle.call_count == 1 and item["active"])
            pump_until(lambda: any(isinstance(widget, tk.Label) and widget.cget("text") == "Second test verse" for widget in widgets))
            verse = next(widget for widget in widgets if isinstance(widget, tk.Label) and widget.cget("text") == "Second test verse")
            click(verse, 2000)
            assert selection.call_count == 0
            click(verse, 2100)
            pump_until(lambda: selection.call_count == 1)
            assert item["selected_verse_index"] == 1
            first = next(widget for widget in widgets if isinstance(widget, tk.Label) and widget.cget("text") == "First test verse")
            show = next(widget for widget in first.master.winfo_children() if isinstance(widget, tk.Button))
            show.invoke()
            pump_until(lambda: selection.call_count == 2)
            assert item["selected_verse_index"] == 0
    finally:
        root.destroy()


def test_scroll_preview_reuses_rasterized_text_and_scales_outline():
    """What: Render many scroll frames without refitting text and invalidate on outline changes.

    Why included: Scroll ticks should move pixels, not repeatedly fit projector-sized text or thicken its preview outline.
    Where used: Mirror scrolling performance and outline regression checks.
    Returns: None; asserts one layout build per typography setting and preview-only background resizing.
    """
    from PIL import Image
    import overlay_layout
    import mirror

    root = tk.Tk()
    root.withdraw()
    original_outline = overlay_layout.get_display_outline_width()
    controller = MirrorDisplayController(
        root, get_app_base_path=lambda: Path("."),
        get_display_config=lambda: {"display_bounds": {"width": 1920, "height": 1080}},
        get_preview_size=lambda: (320, 180), resolve_background_path=lambda path: path,
        serialize_overlay_font=lambda font: None, build_hot_background_paths=lambda paths: [],
        font_value_factory=lambda: ("Segoe UI", 18, "normal"),
    )
    source = Image.new("RGB", (640, 360), "black")
    try:
        overlay_layout.set_display_outline_width("4")
        with patch("overlay_layout.fit_overlay_layout", wraps=overlay_layout.fit_overlay_layout) as fit, \
                patch("mirror.ImageOps.fit", wraps=mirror.ImageOps.fit) as background_fit:
            for offset in range(180, 360, 10):
                image, cycle = controller.render_preview_pil_image(source, "Scrolling welcome text", ("Segoe UI", 18, "normal"),
                    source_value="welcome", scroll_enabled=True, preview_scroll_offset=offset, preview_size=(320, 180))
                assert image.size == (320, 180)
                assert cycle > 180
            assert fit.call_count == 1
            assert all(call.args[1] == (320, 180) for call in background_fit.call_args_list)
            first_layer = controller.preview_scroll_layer_image
            assert first_layer.width == 320
            overlay_layout.set_display_outline_width("0")
            controller.render_preview_pil_image(source, "Scrolling welcome text", ("Segoe UI", 18, "normal"),
                source_value="welcome", scroll_enabled=True, preview_scroll_offset=180, preview_size=(320, 180))
            assert fit.call_count == 2
            assert controller.preview_scroll_layer_image.tobytes() != first_layer.tobytes()
    finally:
        overlay_layout.set_display_outline_width("Auto" if original_outline is None else str(original_outline))
        controller.close()
        root.destroy()


def test_scroll_outline_matches_shared_preview_and_browser_units():
    """What: Compare shared Welcome/mirror scroll pixels and output-to-browser outline units.

    Why included: Preview stroke thickness must scale from the same output-sized typography as OBS.
    Where used: Cross-renderer outline regression checks for Auto, zero, and explicit widths.
    Returns: None; asserts pixel equality, correct CSS conversion, and invalidated cached strips.
    """
    from PIL import Image
    import overlay_layout
    from obs_secondary_display import get_browser_outline_width
    from obs_secondary_display import WELCOME_RENDER_SIZE_BOOST
    from mirror import WELCOME_LAYOUT_SIZE_BOOST

    assert WELCOME_RENDER_SIZE_BOOST == WELCOME_LAYOUT_SIZE_BOOST == 1.15

    root = tk.Tk()
    root.withdraw()
    original_outline = overlay_layout.get_display_outline_width()
    controller = MirrorDisplayController(
        root, get_app_base_path=lambda: Path("."),
        get_display_config=lambda: {"display_bounds": {"width": 1920, "height": 1080}},
        get_preview_size=lambda: (320, 180), resolve_background_path=lambda path: path,
        serialize_overlay_font=lambda font: None, build_hot_background_paths=lambda paths: [],
        font_value_factory=lambda: ("Segoe UI", 24, "normal"),
    )
    background = Image.new("RGB", (320, 180), "black")
    text = "Welcome to our service"
    font = ("Segoe UI", 24, "normal")
    cache = {}
    strips = []
    try:
        for outline in ("0", "4", "Auto"):
            overlay_layout.set_display_outline_width(outline)
            shared, cycle = overlay_layout.render_scrolling_preview(background, text, font, 1920, 1080, 180, cache)
            mirror_image, mirror_cycle = controller.render_cached_scroll_preview(background, text, font, "", 1920, 1080, 180, "")
            assert shared.tobytes() == mirror_image.tobytes()
            assert cycle == mirror_cycle
            layout = overlay_layout.fit_overlay_layout(text, overlay_layout.scale_overlay_font_value(font, 1920, 1080),
                        1920, 1080, scroll_enabled=True, size_boost=1.15)
            assert get_browser_outline_width(layout.font_value[1]) == 2 * overlay_layout.get_effective_stroke_width(layout.font_value[1])
            strips.append(cache["text_layer"].tobytes())
        assert strips[0] != strips[1]
        assert strips[1] != strips[2]
    finally:
        overlay_layout.set_display_outline_width("Auto" if original_outline is None else str(original_outline))
        controller.close()
        root.destroy()


def test_welcome_preview_refreshes_outline_and_uses_shared_scroll_renderer():
    """What: Exercise real Welcome widgets through outline changes and scrolling preview refreshes.

    Why included: A correct renderer alone is insufficient if the tab keeps stale overlays or bypasses it.
    Where used: Welcome static-cache and shared-scroll integration regression checks.
    Returns: None; asserts outline invalidation and delegates scrolling to the shared helper.
    """
    import overlay_layout
    import tab_welcome

    root = tk.Tk()
    root.withdraw()
    original_outline = overlay_layout.get_display_outline_width()
    widgets = []
    welcome_state = {
        "text_size_var": tk.IntVar(root, value=18),
        "combine_text_var": tk.BooleanVar(root, value=False),
        "activate_scroll_var": tk.BooleanVar(root, value=False),
        "scroll_speed_var": tk.IntVar(root, value=7),
    }

    def placeholder(parent, title):
        """What: Build a real, fixed-size preview without main application initialization.

        Why included: The test needs Tk image behavior but not persistence or external output.
        Where used: Injected Welcome preview builder.
        Returns: Preview frame and label.
        """
        frame = tk.Frame(parent, width=320, height=180)
        label = tk.Label(frame, text=title)
        label.pack()
        return frame, label

    try:
        with patch("tab_welcome.load_welcome_notes", return_value=[]), \
                patch("tab_welcome.get_upcoming_birthdays", return_value=[]), \
                patch("tab_welcome.render_text_fields_overlay", wraps=overlay_layout.render_text_fields_overlay) as static, \
                patch("tab_welcome.render_scrolling_preview", wraps=overlay_layout.render_scrolling_preview) as scroll:
            tab_welcome.build_welcome_tab(tk.Frame(root), {
                "tab_widgets": widgets, "tab_labels": [], "tab_inputs": [],
                "build_preview_placeholder": placeholder, "apply_layout_style": lambda *args, **kwargs: None,
                "welcome_state": welcome_state,
                "display_font_family": tk.StringVar(root, value="Segoe UI"),
                "display_font_style": tk.StringVar(root, value="normal"),
                "get_preview_size": lambda: (320, 180),
                "get_display_bounds": lambda: {"width": 1920, "height": 1080},
            })
            editor = next(widget for widget in widgets if isinstance(widget, tk.Text))
            editor.insert("1.0", "Welcome to our service")
            overlay_layout.set_display_outline_width("4")
            welcome_state["refresh_preview"]()
            first_count = static.call_count
            overlay_layout.set_display_outline_width("0")
            welcome_state["refresh_preview"]()
            assert static.call_count == first_count + 1
            welcome_state["activate_scroll_var"].set(True)
            welcome_state["refresh_preview"]()
            assert scroll.call_count == 1
            assert scroll.call_args.args[1] == "Welcome to our service"
            assert scroll.call_args.args[3:5] == (1920, 1080)
            assert scroll.call_args.kwargs["size_boost"] == 1.15
    finally:
        overlay_layout.set_display_outline_width("Auto" if original_outline is None else str(original_outline))
        root.destroy()


def test_secondary_polling_never_overlaps_and_recovers_after_timeout():
    """What: Execute the actual browser polling function with blocked, failed, and timed-out fetches.

    Why included: Repeated state polls must not accumulate requests during long scrolling sessions.
    Where used: OBS browser-state polling regression checks; requires Node.js for JavaScript execution.
    Returns: None; skips when Node.js is unavailable and otherwise asserts bounded polling.
    """
    from obs_secondary_display import SECONDARY_DISPLAY_HTML

    node = shutil.which("node")
    if node is None:
        raise unittest.SkipTest("Node.js is required for the browser polling regression")
    polling = SECONDARY_DISPLAY_HTML.split("let statePollInFlight = false;", 1)[1].split("window.setInterval(pollState, 30);", 1)[0]
    program = """
const assert = require('node:assert/strict');
const timers = new Map();
let timerId = 0;
global.window = {
    setTimeout: callback => { timers.set(++timerId, callback); return timerId; },
    clearTimeout: id => timers.delete(id)
};
const stateUrl = 'http://127.0.0.1/state.json';
let calls = 0;
let complete;
let fail;
let applied = 0;
const applyState = state => { applied++; };
global.fetch = (url, options) => {
    calls++;
    return new Promise((resolve, reject) => {
        complete = () => resolve({ ok: true, json: async () => ({ revision: calls }) });
        fail = () => reject(new Error('controlled failure'));
        options.signal.addEventListener('abort', fail, { once: true });
    });
};
let statePollInFlight = false;
""" + polling + """
(async () => {
    const first = pollState();
    for (let index = 0; index < 100; index++) await pollState();
    assert.equal(calls, 1);
    complete();
    await first;
    assert.equal(applied, 1);
    assert.equal(timers.size, 0);
    const failure = pollState();
    fail();
    await failure;
    assert.equal(statePollInFlight, false);
    const timeout = pollState();
    timers.get(timerId)();
    await timeout;
    assert.equal(statePollInFlight, false);
    assert.equal(timers.size, 0);
    const recovery = pollState();
    complete();
    await recovery;
    assert.equal(applied, 2);
    console.log('PASS: one in-flight poll, timeout cleanup, and recovery.');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run([node, "-e", program], capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_mirror_preview_keeps_one_worker_and_latest_pending_frame():
    """What: Block one scroll render while many newer requests arrive and verify bounded work.

    Why included: Long-running animations must not accumulate preview threads or obsolete frames.
    Where used: Mirror scroll/GIF worker regression checks.
    Returns: None; asserts only the first and newest requested frames are rendered.
    """
    from PIL import Image

    root = tk.Tk()
    root.withdraw()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    rendered = []
    worker_threads = []
    real_thread = threading.Thread
    controller = MirrorDisplayController(
        root, get_app_base_path=lambda: Path("."),
        get_display_config=lambda: {"display_bounds": {"width": 640, "height": 360}},
        get_preview_size=lambda: (320, 180), resolve_background_path=lambda path: path,
        serialize_overlay_font=lambda font: None, build_hot_background_paths=lambda paths: [],
        font_value_factory=lambda: ("Segoe UI", 12, "normal"),
    )
    controller.preview_container = tk.Frame(root)
    controller.preview_label = tk.Label(controller.preview_container)
    controller.preview_text = "first"
    controller.scroll_enabled = True
    controller.source = "welcome"

    def render_blocked(source, text, font, **kwargs):
        """What: Hold the initial frame until the test has queued all replacements.

        Why included: A controlled slow render exposes growing worker backlogs deterministically.
        Where used: Patched mirror Pillow rendering method.
        Returns: Empty image result and zero cycle height for the UI application path.
        """
        rendered.append(text)
        if text == "first":
            started.set()
            assert release.wait(5)
        if text == "frame-29":
            finished.set()
        return None, 0

    def track_worker(*args, **kwargs):
        """What: Record real rendering threads created by the controller.

        Why included: The regression measures launched workers rather than merely queued tokens.
        Where used: Patched threading.Thread constructor.
        Returns: Real thread object so synchronization remains representative.
        """
        worker = real_thread(*args, **kwargs)
        worker_threads.append(worker)
        return worker

    try:
        with patch.object(controller, "get_cached_preview_background", return_value=Image.new("RGB", (320, 180))), \
                patch.object(controller, "render_preview_pil_image", side_effect=render_blocked), \
                patch("mirror.threading.Thread", side_effect=track_worker):
            controller.request_preview_render()
            assert started.wait(5)
            for index in range(30):
                controller.preview_text = f"frame-{index}"
                controller.request_preview_render()
            assert len(worker_threads) == 1
            release.set()
            assert finished.wait(5)
            worker_threads[0].join(timeout=5)
            assert not worker_threads[0].is_alive()
            assert rendered == ["first", "frame-29"]
            assert controller.pending_preview_render is None
            assert controller.preview_render_worker_active is False
    finally:
        release.set()
        for worker in worker_threads:
            worker.join(timeout=5)
        controller.close()
        root.destroy()


def test_preloaded_bible_font_is_reused_by_rendering():
    """What: Warm a queued passage and verify subsequent rendering fitting is a cache hit.

    Why included: Preloading must remove cold font measurement without changing the payload.
    Where used: Bible rendering/preload regression checks.
    Returns: None; assertions fail if activation recomputes the fitted font.
    """
    from overlay_layout import _BIBLE_LOCKED_FONT_CACHE, get_bible_locked_font_value

    columns = [{"title": "Preload Test", "verses": ["A unique verse prepared before activation."], "theme_header": "Preloaded Theme"}]
    payload = {"display_columns": columns, "font_value": ("Segoe UI", 16, "normal")}
    _BIBLE_LOCKED_FONT_CACHE.clear()
    preload_bible_payloads([payload], 640, 360)
    with patch("overlay_layout.get_pil_font", side_effect=AssertionError("Cold font measurement during activation")):
        fitted = get_bible_locked_font_value(columns, payload["font_value"], 640, 360)
    assert fitted[1] > 0
    assert payload["display_columns"] == columns
    assert "active" not in payload


def test_hidden_display_preloads_sent_and_recalled_inactive_bibles():
    """What: Load sent/recalled queue snapshots while Display is hidden and capture preload work.

    Why included: Users should not need to open Display or activate a Bible to prepare its layout.
    Where used: Shared bridge-refresh regression checks for Bible sending and saved-service recall.
    Returns: None; asserts all passages/settings are captured without making any Bible live.
    """
    root = tk.Tk()
    root.withdraw()
    notebook = ttk.Notebook(root)
    display_frame = tk.Frame(notebook)
    bible_frame = tk.Frame(notebook)
    notebook.add(display_frame, text="Display")
    notebook.add(bible_frame, text="Bible")
    notebook.select(bible_frame)
    display_state = {}
    captured = []
    live_payloads = []
    sent = {"id": "sent-bible", "source": "bible", "active": False, "title": "Sent Bible", "verses": ["Sent verse"],
            "display_columns": [{"title": "KJV", "verses": ["Sent verse"]}], "font_value": ["Segoe UI", 12, "normal"]}
    recalled = dict(sent, id="recalled-bible", title="Recalled Bible", verses=["Recalled verse"],
                    display_columns=[{"title": "AFR", "verses": ["Recalled verse"]}])
    bridge_state = {"queue_items": [sent]}

    def capture_preload(payloads, width, height):
        """What: Record worker requests instead of fitting real text in this routing test.

        Why included: The regression targets hidden-tab routing, not rendering speed.
        Where used: Patched preload_bible_payloads() callback.
        Returns: None.
        """
        captured.append((payloads, width, height))

    def wait_for_preload(item_id):
        """What: Pump Tk until the expected Bible appears in a captured preload request.

        Why included: Queue loads and layout preparation run on separate workers.
        Where used: Sent and recalled snapshot assertions.
        Returns: None; raises if the item was never prepared.
        """
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            root.update()
            if any(any(payload.get("queue_item_id") == item_id for payload in request[0]) for request in captured):
                return
            time.sleep(0.01)
        raise AssertionError("Bible was not preloaded: " + item_id)

    try:
        with patch("tab_display.load_display_queue_state", side_effect=lambda: bridge_state), \
                patch("tab_display.list_saved_service_entries", return_value=[]), \
                patch("tab_display.preload_bible_payloads", side_effect=capture_preload):
            build_display_tab(display_frame, {
                "tab_widgets": [], "tab_labels": [], "tab_inputs": [], "display_state": display_state,
                "apply_layout_style": lambda *args, **kwargs: None, "notebook": notebook,
                "selected_font_family": tk.StringVar(root, value="Segoe UI"),
                "selected_font_style": tk.StringVar(root, value="Bold"),
                "selected_font_size": tk.IntVar(root, value=18),
                "preparation_state": {"get_theme_header": lambda: "Service Theme"},
                "get_display_bounds": lambda: {"width": 640, "height": 360},
                "handle_display_live_change": lambda payload, **kwargs: live_payloads.append(payload),
            })
            display_state["refresh_from_bridge"]()
            wait_for_preload("sent-bible")
            bridge_state = {"queue_items": [recalled]}
            display_state["refresh_from_bridge"]()
            wait_for_preload("recalled-bible")
            assert notebook.nametowidget(notebook.select()) is bible_frame
            assert not any(item.get("active") for item in display_state["get_queue_items"]())
            assert all(payload is None for payload in live_payloads)
            payloads, width, height = captured[-1]
            assert (width, height) == (640, 360)
            assert payloads[0]["font_value"] == ("Segoe UI", 18, "bold")
            assert payloads[0]["display_columns"][0]["theme_header"] == "Service Theme"
    finally:
        root.destroy()


def test_bible_locked_font_matches_original_pixel_fitting():
    """Compare the cached Bible font against independent pixel-fit calculations.

    What: Fits every verse across multiple viewport sizes and checks that
    the shared locked font equals the tightest resulting size.
    Why included: Verifies binary-search fitting against a linear pixel check
    without allowing translation headings to reduce the verse font.
    Where used: pytest regression suite for overlay_layout Bible font fitting.
    Returns: None; an assertion failure indicates a fitting regression.
    """
    from overlay_layout import DEFAULT_STROKE_WIDTH, SECONDARY_LAYOUT_SIZE_BOOST, get_bible_locked_font_value, get_line_spacing, get_pil_font, get_text_bbox, scale_overlay_font_value, wrap_text_to_width

    columns = [
        {"title": "KJV", "verses": ["Short verse", "A complete longer verse with several words. " * 12]},
        {"title": "Afrikaans Bible", "verses": ["Kort vers", "Nog 'n volledige vers om die uitleg te toets. " * 8]},
    ]
    font_value = ("Segoe UI", 12, "bold")
    for width, height in ((640, 360), (1200, 675)):
        scaled = scale_overlay_font_value(font_value, width, height)
        region_width = width // len(columns) - 12
        title_height = max(24, round(height * 0.18))
        body_height = max(24, height - title_height - 36)
        usable_width = max(60, region_width - (DEFAULT_STROKE_WIDTH + 12) * 2)
        usable_height = max(60, body_height - (DEFAULT_STROKE_WIDTH + 12) * 2)
        requested = max(10, round(scaled[1] * SECONDARY_LAYOUT_SIZE_BOOST))
        maximum = max(18, min(round(body_height * 0.22), max(18, requested)))
        sizes = []
        for column in columns:
            for verse in column["verses"]:
                fitted_size = 10
                for size in range(maximum, 9, -2):
                    font = get_pil_font((scaled[0], size, scaled[2]))
                    wrapped = wrap_text_to_width(verse, font, usable_width, DEFAULT_STROKE_WIDTH)
                    bounds = get_text_bbox(wrapped, font, get_line_spacing(size, 0.14))
                    if bounds[3] - bounds[1] <= usable_height:
                        fitted_size = size
                        break
                sizes.append(fitted_size)
        actual = get_bible_locked_font_value(columns, font_value, width, height)
        assert actual == ("Segoe UI", max(10, min(sizes)), "bold")
        long_titles = [dict(column, title="A deliberately long translation heading " * 6) for column in columns]
        assert get_bible_locked_font_value(long_titles, font_value, width, height) == actual


def test_locked_bible_overlay_preserves_verse_and_font():
    """Verify verse selection changes rendered pixels without changing locked size.

    What: Renders two selected verses with the same locked font and compares images
    while asserting the computed locked font remains stable.
    Why included: Selection changes should repaint content, not resize Bible text.
    Where used: pytest regression suite for Bible overlay rendering.
    Returns: None; assertions fail if the output is blank, identical, or resized.
    """
    from PIL import Image
    from overlay_layout import get_bible_locked_font_value, render_bible_columns_overlay

    columns = [{"title": "KJV", "verses": ["First complete verse.", "Second complete verse with different text."]}]
    font = get_bible_locked_font_value(columns, ("Segoe UI", 12, "normal"), 640, 360)
    background = Image.new("RGB", (640, 360))
    first = render_bible_columns_overlay(640, 360, columns, 0, font, background, locked_font_value=font)
    second = render_bible_columns_overlay(640, 360, columns, 1, font, background, locked_font_value=font)
    assert first.getbbox() and second.getbbox()
    assert first.tobytes() != second.tobytes()
    assert get_bible_locked_font_value(columns, ("Segoe UI", 12, "normal"), 640, 360) == font


def test_bible_preview_signature_changes_only_when_bible_content_changes():
    """Verify Bible preview signatures distinguish verse selection deterministically.

    What: Builds signatures for different and identical selected indices from one payload.
    Why included: Preview caching must refresh when selected Bible content changes,
    but remain stable for the same content.
    Where used: pytest regression suite for tab_display preview signatures.
    Returns: None; assertions fail when signatures are stale or unstable.
    """
    payload = {
        "source_name": "KJV",
        "book_name": "Genesis",
        "chapter_number": 1,
        "range_label": "1-2",
        "page_labels": ["1-2"],
        "verse_numbers": [1, 2],
        "display_columns": [
            {"title": "KJV", "verses": ["In the beginning", "God created"]},
            {"title": "AFR", "verses": ["In die begin", "God het geskep"]},
        ],
    }

    assert build_bible_preview_signature(payload, 0) != build_bible_preview_signature(payload, 1)
    assert build_bible_preview_signature(payload, 0) == build_bible_preview_signature(payload, 0)


def test_bible_theme_header_is_shared_across_columns():
    """Check that the shared Bible theme header renders identically for 1-3 columns.

    What: Renders overlays at several column counts and compares the header-band
    pixels while verifying both header and body regions contain output.
    Why included: Column count must not change the reserved shared-header geometry.
    Where used: pytest regression suite for overlay_layout multi-column rendering.
    Returns: None; assertions fail on blank output or inconsistent header bands.
    """
    from PIL import Image
    from overlay_layout import get_bible_locked_font_value, render_bible_columns_overlay

    background = Image.new("RGB", (960, 540))
    header_bands = []
    for count in (1, 2, 3):
        columns = [{"title": f"Bible {index}", "verses": ["A complete verse."], "theme_header": "Our Service Theme" if index == 0 else ""} for index in range(count)]
        font = get_bible_locked_font_value(columns, ("Segoe UI", 12, "normal"), 960, 540)
        image = render_bible_columns_overlay(960, 540, columns, 0, ("Segoe UI", 36, "normal"), background, locked_font_value=font)
        header_height = round(540 * 0.18)
        header = image.crop((0, 0, 960, header_height))
        assert header.getbbox()
        assert image.crop((0, header_height, 960, 540)).getbbox()
        header_bands.append(header.tobytes())
    assert header_bands[0] == header_bands[1] == header_bands[2]


def test_bible_payload_keeps_column_structure_and_selected_index():
    """Verify the mirror controller retains Bible columns and selected verse state.

    What: Sends a Bible payload to a real controller instance and checks both columns,
    selected index, and compatibility preview text.
    Why included: Mirror synchronization must preserve structured Bible data instead
    of flattening it to generic text.
    Where used: pytest regression suite for MirrorDisplayController payload handling.
    Returns: None; always destroys the temporary Tk root, and assertions verify state.
    """
    root = tk.Tk()
    root.withdraw()
    try:
        controller = MirrorDisplayController(
            root,
            get_app_base_path=lambda: Path("."),
            get_display_config=lambda: {"display_bounds": {"width": 1920, "height": 1080}},
            get_preview_size=lambda: (320, 180),
            resolve_background_path=lambda path: Path(path),
            serialize_overlay_font=lambda value: {"family": "Segoe UI", "size": 24, "style": "normal"},
            build_hot_background_paths=lambda paths: [str(p) for p in paths if p],
            font_value_factory=lambda: ("Segoe UI", 24, "normal"),
        )

        payload = {
            "text": "Verse 2",
            "source": "bible",
            "display_columns": [
                {"title": "KJV", "verses": ["Verse 1", "Verse 2"]},
                {"title": "AFR", "verses": ["Vers 1", "Vers 2"]},
            ],
            "selected_verse_index": 1,
            "scroll_enabled": False,
            "scroll_speed": 7,
            "background_path": "Resourses/PewVista.png",
        }

        controller.set_live_payload(payload, force=True)

        assert controller.display_columns[0]["title"] == "KJV"
        assert controller.display_columns[1]["title"] == "AFR"
        assert controller.selected_verse_index == 1
        assert controller.preview_text == "Verse 2"
    finally:
        root.destroy()


def test_display_tab_keeps_generic_bible_selection_controls():
    """Ensure Bible queue items do not enable the separate multi-column detail UI.

    What: Passes a structured Bible queue item to the display-details predicate.
    Why included: Bible content should retain the generic queue selection and verse
    controls in the Display tab.
    Where used: pytest regression suite for tab_display control selection.
    Returns: None; asserts the specialized multi-column controls are not selected.
    """
    queue_item = {
        "source": "bible",
        "title": "John 1",
        "display_columns": [
            {"title": "KJV", "verses": ["In the beginning", "The word was with God"]},
            {"title": "AFR", "verses": ["In die begin", "Die Woord was by God"]},
        ],
        "verses": ["In the beginning", "The word was with God"],
    }

    assert should_render_bible_columns_in_display_details(queue_item) is False


def test_translation_preview_rows_show_word_meanings_even_before_cache_is_loaded():
    """Verify translation rows retain source words before meaning data is available.

    What: Checks populated translation mappings and an empty mapping against expected
    source/translated/meaning row tuples.
    Why included: A not-yet-loaded word-meaning cache must not hide source text.
    Where used: pytest regression suite for tab_translation preview-row generation.
    Returns: None; assertions compare generated rows to expected values.
    """
    import tab_translation as tt

    rows = tt.build_verse_meaning_rows(
        "In the beginning God created",
        {
            "in": {"afr": "In", "eng": "in"},
            "the": {"afr": "die", "eng": "the"},
            "beginning": {"afr": "begin", "eng": "beginning"},
            "god": {"afr": "God", "eng": "God"},
            "created": {"afr": "geskep", "eng": "created"},
        },
        "en",
    )

    assert rows == [
        ("In", "In", "in"),
        ("the", "die", "the"),
        ("beginning", "begin", "beginning"),
        ("God", "God", "God"),
        ("created", "geskep", "created"),
    ]

    empty_rows = tt.build_verse_meaning_rows("In the beginning", {}, "en")
    assert empty_rows == [
        ("In", "", ""),
        ("the", "", ""),
        ("beginning", "", ""),
    ]
