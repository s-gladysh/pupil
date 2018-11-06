"""
(*)~---------------------------------------------------------------------------
Pupil - eye tracking platform
Copyright (C) 2012-2018 Pupil Labs

Distributed under the terms of the GNU
Lesser General Public License (LGPL v3.0).
See COPYING and COPYING.LESSER for license details.
---------------------------------------------------------------------------~(*)
"""

import os
import logging
import time
import platform
import multiprocessing
import csv
import itertools

logger = logging.getLogger(__name__)
if platform.system() == "Darwin":
    mp = multiprocessing.get_context("fork")
else:
    mp = multiprocessing.get_context()

import numpy as np
import cv2
import pyglui
import gl_utils
import pyglui.cygl.utils as pyglui_utils
import OpenGL.GL as gl

from plugin import Analysis_Plugin_Base
import file_methods
from cache_list import Cache_List
import player_methods

from surface_tracker.surface_tracker import Surface_Tracker
from surface_tracker import (
    offline_utils,
    background_tasks,
    Square_Marker_Detection,
    Heatmap_Mode,
)
from surface_tracker.surface_offline import Surface_Offline


class Surface_Tracker_Offline(Surface_Tracker, Analysis_Plugin_Base):
    """
    # TODO update docstring
    - Mostly extends the Surface Tracker with a cache
    Special version of surface tracker for use with videofile source.
    It uses a seperate process to search all frames in the world video file for markers.
     - self.cache is a list containing marker positions for each frame.
     - self.surfaces[i].cache is a list containing surface positions for each frame
    Both caches are build up over time. The marker cache is also session persistent.
    See marker_tracker.py for more info on this marker tracker.
    """

    order = 0.2

    def __init__(self, g_pool, marker_min_perimeter=60, inverted_markers=False):
        self.timeline_line_height = 16
        super().__init__(g_pool, marker_min_perimeter, inverted_markers)

        self.MARKER_CACHE_VERSION = 3
        # Also add very small detected markers to cache and filter cache afterwards
        self.CACHE_MIN_MARKER_PERIMETER = 20
        self.cache_seek_idx = mp.Value("i", 0)
        self.marker_cache = None
        self.marker_cache_unfiltered = None
        self.cache_filler = None
        self._init_marker_cache()
        self.last_cache_update_ts = time.time()
        self.CACHE_UPDATE_INTERVAL_SEC = 5

        self.gaze_on_surf_buffer = None
        self.gaze_on_surf_buffer_filler = None
        self.fixations_on_surf_buffer = None
        self.fixations_on_surf_buffer_filler = None

        self._heatmap_update_requests = set()
        self.make_export = False
        self.export_params = None

    @property
    def Surface_Class(self):
        return Surface_Offline

    @property
    def _save_dir(self):
        return self.g_pool.rec_dir

    @property
    def has_freeze_feature(self):
        return False

    @property
    def ui_info_text(self):
        return (
            "The offline surface tracker will look for markers in the entire "
            "video. By default it uses surfaces defined in capture. You can "
            "change and add more surfaces here. \n \n Press the export button or "
            "type 'e' to start the export."
        )

    @property
    def supported_heatmap_modes(self):
        return [Heatmap_Mode.WITHIN_SURFACE, Heatmap_Mode.ACROSS_SURFACES]

    def _init_marker_cache(self):
        previous_cache = file_methods.Persistent_Dict(
            os.path.join(self.g_pool.rec_dir, "square_marker_cache")
        )
        version = previous_cache.get("version", 0)
        cache = previous_cache.get("marker_cache_unfiltered", None)

        if cache is None:
            self._recalculate_marker_cache()
        elif version != self.MARKER_CACHE_VERSION:
            logger.debug("Marker cache version missmatch. Rebuilding marker cache.")
            self.inverted_markers = previous_cache.get("inverted_markers", False)
            self._recalculate_marker_cache()
        else:
            marker_cache_unfiltered = []
            for markers in cache:
                # Loaded markers are either False, [] or a list of dictionaries. We
                # need to convert the dictionaries into Square_Marker_Detection objects.
                if markers:

                    markers = [
                        Square_Marker_Detection(*args) if args else None
                        for args in markers
                    ]
                marker_cache_unfiltered.append(markers)

            self._recalculate_marker_cache(previous_state=marker_cache_unfiltered)
            self.inverted_markers = previous_cache.get("inverted_markers", False)
            logger.debug("Restored previous marker cache.")

    def _recalculate_marker_cache(self, previous_state=None):
        if previous_state is None:
            previous_state = [False for _ in self.g_pool.timestamps]

            # If we had a previous_state argument, surface objects had just been
            # initialized with their previous state, which we do not want to overwrite.
            # Therefore resetting the marker cache is only done when no previous_state
            # is defined.
            for surface in self.surfaces:
                surface.location_cache = None

        self.marker_cache_unfiltered = Cache_List(
            previous_state, positive_eval_fn=_cache_positive_eval_fn
        )
        self._update_filtered_markers()

        self.cache_filler = background_tasks.background_video_processor(
            self.g_pool.capture.source_path,
            offline_utils.marker_detection_callable(
                self.CACHE_MIN_MARKER_PERIMETER, self.inverted_markers
            ),
            list(self.marker_cache),
            self.cache_seek_idx,
        )

    def _update_filtered_markers(self):
        marker_cache = []
        for markers in self.marker_cache_unfiltered:
            if markers:
                markers = self._filter_markers(markers)
            marker_cache.append(markers)
        self.marker_cache = Cache_List(
            marker_cache, positive_eval_fn=_cache_positive_eval_fn
        )

    def init_ui(self):
        super().init_ui()

        self.glfont = pyglui.pyfontstash.fontstash.Context()
        self.glfont.add_font("opensans", pyglui.ui.get_opensans_font_path())
        self.glfont.set_color_float((1.0, 1.0, 1.0, 0.8))
        self.glfont.set_align_string(v_align="right", h_align="top")

        self.timeline = pyglui.ui.Timeline(
            "Surface Tracker",
            self._gl_display_cache_bars,
            self._draw_labels,
            self.timeline_line_height * (len(self.surfaces) + 1),
        )
        self.g_pool.user_timelines.append(self.timeline)
        self.timeline.content_height = (
            len(self.surfaces) + 1
        ) * self.timeline_line_height

    def recent_events(self, events):
        super().recent_events(events)
        self._fetch_data_from_bg_fillers()

    def _fetch_data_from_bg_fillers(self):
        if self.gaze_on_surf_buffer_filler is not None:
            for gaze in self.gaze_on_surf_buffer_filler.fetch():
                try:
                    self.gaze_on_surf_buffer.append(gaze)
                except AttributeError:
                    self.gaze_on_surf_buffer = []
                    self.gaze_on_surf_buffer.append(gaze)

            # fixations will be gathered additionally to gaze if we want to make an export
            if self.fixations_on_surf_buffer_filler is not None:
                for fixation in self.fixations_on_surf_buffer_filler.fetch():
                    try:
                        self.fixations_on_surf_buffer.append(fixation)
                    except AttributeError:
                        self.fixations_on_surf_buffer = []
                        self.fixations_on_surf_buffer.append(fixation)

            # Once all background processes are completed, update and export!
            if self.gaze_on_surf_buffer_filler.completed and (
                self.fixations_on_surf_buffer_filler is None
                or self.fixations_on_surf_buffer_filler.completed
            ):
                self.gaze_on_surf_buffer_filler = None
                self.fixations_on_surf_buffer_filler = None
                self._update_surface_heatmaps()
                if self.make_export:
                    self.save_surface_statisics_to_file()
                self.gaze_on_surf_buffer = None
                self.fixations_on_surf_buffer = None

    def _update_markers(self, frame):
        self._update_marker_and_surface_caches()

        self.markers = self.marker_cache[frame.index]
        self.markers_unfiltered = self.marker_cache_unfiltered[frame.index]
        if self.markers is False:
            # Move seek index to current frame because caches do not contain data for it
            self.markers = []
            self.markers_unfiltered = []
            self.cache_seek_idx.value = frame.index

    def _update_marker_and_surface_caches(self):
        if self.cache_filler is None:
            return

        for frame_index, markers in self.cache_filler.fetch():
            markers = self._remove_duplicate_markers(markers)
            self.marker_cache_unfiltered.update(frame_index, markers)
            markers_filtered = self._filter_markers(markers)
            self.marker_cache.update(frame_index, markers_filtered)

            for surface in self.surfaces:
                surface.update_location_cache(
                    frame_index, self.marker_cache, self.camera_model
                )

        if self.cache_filler.completed:
            self.cache_filler = None
            for surface in self.surfaces:
                self._heatmap_update_requests.add(surface)
            self._fill_gaze_on_surf_buffer()
            self._save_marker_cache()
            self.save_surface_definitions_to_file()

        now = time.time()
        if now - self.last_cache_update_ts > self.CACHE_UPDATE_INTERVAL_SEC:
            self._save_marker_cache()
            self.last_cache_update_ts = now

    def _update_surface_locations(self, frame_index):
        for surface in self.surfaces:
            surface.update_location(frame_index, self.marker_cache, self.camera_model)

    def _update_surface_corners(self):
        for surface, corner_idx in self._edit_surf_verts:
            if surface.detected:
                surface.move_corner(
                    self.current_frame.index,
                    self.marker_cache,
                    corner_idx,
                    self._last_mouse_pos.copy(),
                    self.camera_model,
                )

    def _update_surface_heatmaps(self):
        self._compute_across_surfaces_heatmap()

        for surface in self._heatmap_update_requests:
            surf_idx = self.surfaces.index(surface)
            gaze_on_surf = self.gaze_on_surf_buffer[surf_idx]
            gaze_on_surf = list(itertools.chain.from_iterable(gaze_on_surf))
            surface.update_heatmap(gaze_on_surf)

        self._heatmap_update_requests.clear()

    def _compute_across_surfaces_heatmap(self):
        gaze_counts_per_surf = []
        for gaze in self.gaze_on_surf_buffer:
            gaze = list(itertools.chain.from_iterable(gaze))
            gaze = [g for g in gaze if g["on_surf"]]
            gaze_counts_per_surf.append(len(gaze))

        if gaze_counts_per_surf:
            max_count = max(gaze_counts_per_surf)
            results = np.array(gaze_counts_per_surf, dtype=np.float32)
            if max_count > 0:
                results *= 255.0 / max_count
            results = np.uint8(results)
            results_color_maps = cv2.applyColorMap(results, cv2.COLORMAP_JET)

            for surface, color_map in zip(self.surfaces, results_color_maps):
                heatmap = np.ones((1, 1, 4), dtype=np.uint8) * 125
                heatmap[:, :, :3] = color_map
                surface.across_surface_heatmap = heatmap
        else:
            for surface in self.surfaces:
                surface.across_surface_heatmap = surface.get_uniform_heatmap()

    def _fill_gaze_on_surf_buffer(self):
        in_mark = self.g_pool.seek_control.trim_left
        out_mark = self.g_pool.seek_control.trim_right
        section = slice(in_mark, out_mark)

        all_world_timestamps = self.g_pool.timestamps
        all_gaze_events = self.g_pool.gaze_positions

        self._start_gaze_buffer_filler(all_gaze_events, all_world_timestamps, section)

        if self.make_export:
            all_fixation_events = self.g_pool.fixations

            self._start_fixation_buffer_filler(
                all_fixation_events, all_world_timestamps, section
            )

    def _start_gaze_buffer_filler(self, all_gaze_events, all_world_timestamps, section):
        if self.gaze_on_surf_buffer_filler is not None:
            self.gaze_on_surf_buffer_filler.cancel()
        self.gaze_on_surf_buffer_filler = background_tasks.background_gaze_on_surface(
            self.surfaces,
            section,
            all_world_timestamps,
            all_gaze_events,
            self.camera_model,
        )

    def _start_fixation_buffer_filler(
        self, all_fixation_events, all_world_timestamps, section
    ):
        if self.fixations_on_surf_buffer_filler is not None:
            self.fixations_on_surf_buffer_filler.cancel()
        self.fixations_on_surf_buffer_filler = background_tasks.background_gaze_on_surface(
            self.surfaces,
            section,
            all_world_timestamps,
            all_fixation_events,
            self.camera_model,
        )

    def gl_display(self):
        if self.timeline:
            self.timeline.refresh()
        super().gl_display()

    def _gl_display_cache_bars(self, width, height, scale):
        ts = self.g_pool.timestamps
        with gl_utils.Coord_System(ts[0], ts[-1], height, 0):
            # Lines for areas that have been cached
            cached_ranges = []
            for r in self.marker_cache.visited_ranges:
                cached_ranges += ((ts[r[0]], 0), (ts[r[1]], 0))

            gl.glTranslatef(0, scale * self.timeline_line_height / 2, 0)
            color = pyglui_utils.RGBA(0.8, 0.2, 0.2, 0.8)
            pyglui_utils.draw_polyline(
                cached_ranges, color=color, line_type=gl.GL_LINES, thickness=scale * 4
            )
            cached_ranges = []
            for r in self.marker_cache.positive_ranges:
                cached_ranges += ((ts[r[0]], 0), (ts[r[1]], 0))

            color = pyglui_utils.RGBA(0, 0.7, 0.3, 0.8)
            pyglui_utils.draw_polyline(
                cached_ranges, color=color, line_type=gl.GL_LINES, thickness=scale * 4
            )

            # Lines where surfaces have been found in video
            cached_surfaces = []
            for surface in self.surfaces:
                found_at = []
                if surface.location_cache is not None:
                    for r in surface.location_cache.positive_ranges:  # [[0,1],[3,4]]
                        found_at += ((ts[r[0]], 0), (ts[r[1]], 0))
                    cached_surfaces.append(found_at)

            color = pyglui_utils.RGBA(0, 0.7, 0.3, 0.8)

            for surface in cached_surfaces:
                gl.glTranslatef(0, scale * self.timeline_line_height, 0)
                pyglui_utils.draw_polyline(
                    surface, color=color, line_type=gl.GL_LINES, thickness=scale * 2
                )

    def _draw_labels(self, width, height, scale):
        self.glfont.set_size(self.timeline_line_height * 0.8 * scale)
        self.glfont.draw_text(width, 0, "Marker Cache")
        for idx, s in enumerate(self.surfaces):
            gl.glTranslatef(0, self.timeline_line_height * scale, 0)
            self.glfont.draw_text(width, 0, s.name)

    def add_surface(self, init_dict=None):
        super().add_surface(init_dict)

        try:
            self.timeline.content_height += self.timeline_line_height
            self._fill_gaze_on_surf_buffer()
        except AttributeError:
            pass
        self.surfaces[-1].on_surface_change = self.on_surface_change

    def remove_surface(self, surface):
        super().remove_surface(surface)
        self.timeline.content_height -= self.timeline_line_height

    def on_notify(self, notification):
        super().on_notify(notification)

        if notification["subject"] == "surface_tracker.marker_detection_params_changed":
            self._recalculate_marker_cache()

        elif notification["subject"] == "surface_tracker.marker_min_perimeter_changed":
            self._update_filtered_markers()
            for surface in self.surfaces:
                surface.location_cache = None

        elif notification["subject"] == "surface_tracker.heatmap_params_changed":
            for surface in self.surfaces:
                if surface.name == notification["name"]:
                    self._heatmap_update_requests.add(surface)
                    surface.within_surface_heatmap = surface.get_placeholder_heatmap()
                    break
            self._fill_gaze_on_surf_buffer()

        elif notification["subject"].startswith("seek_control.trim_indices_changed"):
            for surface in self.surfaces:
                surface.within_surface_heatmap = surface.get_placeholder_heatmap()
                self._heatmap_update_requests.add(surface)
            self._fill_gaze_on_surf_buffer()

        elif notification["subject"] == "surface_tracker.surfaces_changed":
            for surface in self.surfaces:
                if surface.name == notification["name"]:
                    surface.location_cache = None
                    surface.within_surface_heatmap = surface.get_placeholder_heatmap()
                    self._heatmap_update_requests.add(surface)
                    break
            self._fill_gaze_on_surf_buffer()

        elif notification["subject"] == "should_export":
            self.make_export = True
            self.export_params = (notification["range"], notification["export_dir"])
            self._fill_gaze_on_surf_buffer()

        elif notification["subject"] == "gaze_positions_changed":
            for surface in self.surfaces:
                self._heatmap_update_requests.add(surface)
                surface.within_surface_heatmap = surface.get_placeholder_heatmap()
            self._fill_gaze_on_surf_buffer()

    def on_surface_change(self, surface):
        self.save_surface_definitions_to_file()
        self._heatmap_update_requests.add(surface)
        self._fill_gaze_on_surf_buffer()

    def save_surface_statisics_to_file(self):
        """
        between in and out mark

            report: gaze distribution:
                    - total gazepoints
                    - gaze points on surface x
                    - gaze points not on any surface

            report: surface visibility

                - total frames
                - surface x visible framecount

            surface events:
                frame_no, ts, surface "name", "id" enter/exit

            for each surface:
                fixations_on_name.csv
                gaze_on_name_id.csv
                positions_of_name_id.csv

        """
        export_range, export_dir = self.export_params
        metrics_dir = os.path.join(export_dir, "surfaces")
        section = slice(*export_range)
        in_mark = section.start
        out_mark = section.stop
        logger.info("exporting metrics to {}".format(metrics_dir))
        if os.path.isdir(metrics_dir):
            logger.info("Will overwrite previous export for this section")
        else:
            try:
                os.mkdir(metrics_dir)
            except OSError:
                logger.warning("Could not make metrics dir {}".format(metrics_dir))
                return

        self._export_surface_visibility(metrics_dir, section)
        self._export_surface_gaze_distribution(export_range, metrics_dir)
        self._export_surface_events(metrics_dir)

        for surf_idx, surface in enumerate(self.surfaces):
            # Sanitize surface name to include it in the filename
            surface_name = "_" + surface.name.replace("/", "")

            self._export_surface_positions(
                in_mark, metrics_dir, out_mark, surface, surface_name
            )
            self._export_gaze_on_surface(
                in_mark,
                metrics_dir,
                self.gaze_on_surf_buffer[surf_idx],
                surface,
                surface_name,
            )
            self._export_fixations_on_surface(
                in_mark,
                metrics_dir,
                self.fixations_on_surf_buffer[surf_idx],
                surface,
                surface_name,
            )
            self._export_surface_heatmap(
                metrics_dir, surface.within_surface_heatmap, surface_name
            )
            logger.info(
                "Saved surface gaze and fixation data for '{}'".format(surface.name)
            )

        logger.info("Done exporting reference surface data.")
        # TODO enable export of surface image?
        # if s.detected and self.img is not None:
        #     #let save out the current surface image found in video

        #     #here we get the verts of the surface quad in norm_coords
        #     mapped_space_one = np.array(((0,0),(1,0),(1,1),(0,1)),dtype=np.float32).reshape(-1,1,2)
        #     screen_space = cv2.perspectiveTransform(mapped_space_one,s.m_to_screen).reshape(-1,2)
        #     #now we convert to image pixel coods
        #     screen_space[:,1] = 1-screen_space[:,1]
        #     screen_space[:,1] *= self.img.shape[0]
        #     screen_space[:,0] *= self.img.shape[1]
        #     s_0,s_1 = s.real_world_size
        #     #no we need to flip vertically again by setting the mapped_space verts accordingly.
        #     mapped_space_scaled = np.array(((0,s_1),(s_0,s_1),(s_0,0),(0,0)),dtype=np.float32)
        #     M = cv2.getPerspectiveTransform(screen_space,mapped_space_scaled)
        #     #here we do the actual perspective transform of the image.
        #     surf_in_video = cv2.warpPerspective(self.img,M, (int(s.real_world_size['x']),int(s.real_world_size['y'])) )
        #     cv2.imwrite(os.path.join(metrics_dir,'surface'+surface_name+'.png'),surf_in_video)
        #     logger.info("Saved current image as .png file.")
        # else:
        #     logger.info("'%s' is not currently visible. Seek to appropriate frame and repeat this command."%s.name)

        self.make_export = False

    def _export_surface_visibility(self, metrics_dir, section):
        with open(
            os.path.join(metrics_dir, "surface_visibility.csv"),
            "w",
            encoding="utf-8",
            newline="",
        ) as csv_file:
            csv_writer = csv.writer(csv_file, delimiter=",")

            # surface visibility report
            frame_count = len(self.g_pool.timestamps[section])

            csv_writer.writerow(("frame_count", frame_count))
            csv_writer.writerow("")
            csv_writer.writerow(("surface_name", "visible_frame_count"))
            for surface in self.surfaces:
                if surface.location_cache is None:
                    logger.warning(
                        "The surface is not cached. Please wait for the cacher to "
                        "collect data."
                    )
                    return
                visible_count = surface.visible_count_in_section(section)
                csv_writer.writerow((surface.name, visible_count))
            logger.info("Created 'surface_visibility.csv' file")

    def _export_surface_gaze_distribution(self, export_range, metrics_dir):
        with open(
            os.path.join(metrics_dir, "surface_gaze_distribution.csv"),
            "w",
            encoding="utf-8",
            newline="",
        ) as csv_file:
            csv_writer = csv.writer(csv_file, delimiter=",")

            # gaze distribution report
            export_window = player_methods.exact_window(
                self.g_pool.timestamps, export_range
            )
            gaze_in_section = self.g_pool.gaze_positions.by_ts_window(export_window)
            not_on_any_surf_ts = set([gp["timestamp"] for gp in gaze_in_section])

            csv_writer.writerow(("total_gaze_point_count", len(gaze_in_section)))
            csv_writer.writerow("")
            csv_writer.writerow(("surface_name", "gaze_count"))

            for surf_idx, surface in enumerate(self.surfaces):
                gaze_on_surf = self.gaze_on_surf_buffer[surf_idx]
                gaze_on_surf = list(itertools.chain.from_iterable(gaze_on_surf))
                gaze_on_surf_ts = set(
                    [gp["base_data"][1] for gp in gaze_on_surf if gp["on_surf"]]
                )
                not_on_any_surf_ts -= gaze_on_surf_ts
                csv_writer.writerow((surface.name, len(gaze_on_surf_ts)))

            csv_writer.writerow(("not_on_any_surface", len(not_on_any_surf_ts)))
            logger.info("Created 'surface_gaze_distribution.csv' file")

    def _export_surface_events(self, metrics_dir):
        with open(
            os.path.join(metrics_dir, "surface_events.csv"),
            "w",
            encoding="utf-8",
            newline="",
        ) as csv_file:
            csv_writer = csv.writer(csv_file, delimiter=",")

            # surface events report
            csv_writer.writerow(
                ("world_index", "world_timestamp", "surface_name", "event_type")
            )

            events = []
            for surface in self.surfaces:
                for (
                    enter_frame_id,
                    exit_frame_id,
                ) in surface.location_cache.positive_ranges:
                    events.append(
                        {
                            "frame_id": enter_frame_id,
                            "surf_name": surface.name,
                            "event": "enter",
                        }
                    )
                    events.append(
                        {
                            "frame_id": exit_frame_id,
                            "surf_name": surface.name,
                            "event": "exit",
                        }
                    )

            events.sort(key=lambda x: x["frame_id"])
            for e in events:
                csv_writer.writerow(
                    (
                        e["frame_id"],
                        self.g_pool.timestamps[e["frame_id"]],
                        e["surf_name"],
                        e["event"],
                    )
                )
            logger.info("Created 'surface_events.csv' file")

    def _export_surface_heatmap(self, metrics_dir, heatmap, surface_name):
        if heatmap is not None:
            logger.info("Saved Heatmap as .png file.")
            cv2.imwrite(
                os.path.join(metrics_dir, "heatmap" + surface_name + ".png"), heatmap
            )

    def _export_surface_positions(
        self, in_mark, metrics_dir, out_mark, surface, surface_name
    ):
        with open(
            os.path.join(metrics_dir, "surf_positions" + surface_name + ".csv"),
            "w",
            encoding="utf-8",
            newline="",
        ) as csv_file:
            csv_writer = csv.writer(csv_file, delimiter=",")
            csv_writer.writerow(
                (
                    "world_index",
                    "world_timestamp",
                    "img_to_surf_trans",
                    "surf_to_img_trans",
                    "num_detected_markers",
                )
            )
            for idx, ts, ref_surf_data in zip(
                range(len(self.g_pool.timestamps)),
                self.g_pool.timestamps,
                surface.location_cache,
            ):
                if in_mark <= idx < out_mark:
                    if (
                        ref_surf_data is not None
                        and ref_surf_data is not False
                        and ref_surf_data.detected
                    ):
                        csv_writer.writerow(
                            (
                                idx,
                                ts,
                                ref_surf_data.img_to_surf_trans,
                                ref_surf_data.surf_to_img_trans,
                                ref_surf_data.num_detected_markers,
                            )
                        )

    def _export_gaze_on_surface(
        self, in_mark, metrics_dir, gazes_on_surface, surface, surface_name
    ):
        with open(
            os.path.join(
                metrics_dir, "gaze_positions_on_surface" + surface_name + ".csv"
            ),
            "w",
            encoding="utf-8",
            newline="",
        ) as csv_file:
            csv_writer = csv.writer(csv_file, delimiter=",")
            csv_writer.writerow(
                (
                    "world_timestamp",
                    "world_index",
                    "gaze_timestamp",
                    "x_norm",
                    "y_norm",
                    "x_scaled",
                    "y_scaled",
                    "on_surf",
                    "confidence",
                )
            )
            for idx, gaze_on_surf in enumerate(gazes_on_surface):
                idx += in_mark
                if gaze_on_surf:
                    for gp in gaze_on_surf:
                        csv_writer.writerow(
                            (
                                self.g_pool.timestamps[idx],
                                idx,
                                gp["timestamp"],
                                gp["norm_pos"][0],
                                gp["norm_pos"][1],
                                gp["norm_pos"][0] * surface.real_world_size["x"],
                                gp["norm_pos"][1] * surface.real_world_size["y"],
                                gp["on_surf"],
                                gp["confidence"],
                            )
                        )

    def _export_fixations_on_surface(
        self, in_mark, metrics_dir, fixations_on_surf, surface, surface_name
    ):
        with open(
            os.path.join(metrics_dir, "fixations_on_surface" + surface_name + ".csv"),
            "w",
            encoding="utf-8",
            newline="",
        ) as csv_file:
            csv_writer = csv.writer(csv_file, delimiter=",")
            csv_writer.writerow(
                (
                    "start_timestamp",
                    "norm_pos_x",
                    "norm_pos_y",
                    "x_scaled",
                    "y_scaled",
                    "on_surf",
                )
            )
            for idx, fix_on_surf in enumerate(fixations_on_surf):
                idx += in_mark
                if fix_on_surf:
                    without_duplicates = dict(
                        [(fix["base_data"][1], fix) for fix in fix_on_surf]
                    ).values()
                    for fix in without_duplicates:
                        csv_writer.writerow(
                            (
                                self.g_pool.timestamps[idx],
                                idx,
                                fix["timestamp"],
                                fix["norm_pos"][0],
                                fix["norm_pos"][1],
                                fix["norm_pos"][0] * surface.real_world_size["x"],
                                fix["norm_pos"][1] * surface.real_world_size["y"],
                                fix["on_surf"],
                                fix["confidence"],
                            )
                        )

    def deinit_ui(self):
        super().deinit_ui()
        self.g_pool.user_timelines.remove(self.timeline)
        self.timeline = None
        self.glfont = None

    def cleanup(self):
        super().cleanup()
        self._save_marker_cache()

    def _save_marker_cache(self):
        marker_cache_file = file_methods.Persistent_Dict(
            os.path.join(self.g_pool.rec_dir, "square_marker_cache")
        )
        marker_cache_file["marker_cache_unfiltered"] = list(
            self.marker_cache_unfiltered
        )
        marker_cache_file["version"] = self.MARKER_CACHE_VERSION
        marker_cache_file["inverted_markers"] = self.inverted_markers
        marker_cache_file.save()


def _cache_positive_eval_fn(x):
    return (x is not False) and len(x) > 0
