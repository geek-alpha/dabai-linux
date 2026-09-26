import { App } from './js/core/app-state.ts';

import init_00_quiet from './js/core/00_quiet.ts';
import init_01_start from './js/core/01_start.ts';
import init_02_three_scene from "./js/core/02_three_scene.ts";
import init_03_model_load_gltf_vrm from './js/core/03_model_load_gltf_vrm.ts';
import init_04_bg_load from './js/character/04_bg_load.ts';
import init_05_move_mode from './js/core/05_move_mode.ts';
import init_06_fpv_mode from './js/character/06_fpv_mode.ts';
import init_07_click_interact from "./js/character/07_click_interact.ts";
import init_08b_expression_engine from './js/character/08_expression_engine.ts';
import init_09_emotion_controller from './js/character/09_emotion_controller.ts';
import init_09b_motion_blender from './js/character/09b_motion_blender.ts';
import init_09c_mixamo_retarget from './js/character/09c_mixamo_retarget.ts';
import init_09d_mixamo_library_loader from './js/character/09d_mixamo_library_loader.ts';
import init_09e_mixamo_emotion_bridge from './js/character/09e_mixamo_emotion_bridge.ts';
import init_08_state_switch from './js/core/08_state_switch.ts';
import init_09_websocket from './js/network/09_websocket.ts';
import init_10_tts_lipsync from './js/core/10_tts_lipsync.ts';
import init_11_voice_record from './js/audio/11_voice_record.ts';
import init_12_vad_auto from './js/core/12_vad_auto.ts';
import init_12b_silero_vad from './js/core/12b_silero_vad.ts';
import init_13_messages from './js/ui/13_messages.ts';
import init_14_toast from './js/ui/14_toast.ts';
import init_15_model_ui from './js/ui/15_model_ui.ts';
import init_16_bg_ui from './js/ui/16_bg_ui.ts';
import init_17_events from './js/ui/17_events.ts';
import init_18_tts_settings from './js/audio/18_tts_settings.ts';
import init_19_boot from './js/core/19_boot.ts';
import init_20_bgm_player from './js/audio/20_bgm_player.ts';
import init_28_music_ui from './js/audio/28_music_ui.ts';
import init_29_video_ui from './js/audio/29_video_ui.ts';
import init_32_workspace_ui from './js/audio/32_workspace_ui.ts';
import init_23_name_settings from './js/ui/23_name_settings.ts';
import init_24_footstep_sfx from './js/audio/24_footstep_sfx.ts';
import init_25_character_cards from "./js/ui/25_character_cards.ts";
import init_25b_llm_providers from "./js/ui/26_llm_providers.ts";
import init_26_webxr_vr from "./js/vr/webxr-vr.ts";
import init_28_codex_runner from "./js/codex/28_codex_runner.ts";
import init_29_task_center from './js/ui/27_task_center.ts';
import init_30_task_tree from './js/ui/30_task_tree.ts';
import init_30_task_big_screen from "./js/ui/30_task_big_screen.ts";
import init_31_tool_chain from './js/ui/31_tool_chain.ts';
import init_34_game_fx from './js/ui/34_game_fx.ts';
import init_ai_autonomy from './js/ai/init-ai-autonomy.ts';
import init_32_vr_ray from './js/vr/vr-ray.ts';
import init_32_vr_hud from './js/vr/vr-hud.ts';
import init_32_vr_ui from './js/vr/vr-ui.ts';
import init_32_vr_toolbar from './js/vr/vr-toolbar.ts';
import init_32_vr_chat from './js/vr/vr-chat.ts';
import init_32_vr_music from './js/vr/vr-music.ts';
import init_32_vr_video from './js/vr/vr-video.ts';
import init_32_vr_fx from './js/vr/vr-fx.ts';
import init_32_vr_holo from './js/vr/vr-holo.ts';
import init_32_vr_voice from './js/vr/vr-voice.ts';
import init_33_stage_wheel from './js/ui/33_stage_wheel.ts';
import init_35_arcade_fx from './js/ui/35_arcade_fx.ts';
import init_36_layout_sync from './js/ui/36_layout_sync.ts';
import init_37_stream_pulse from './js/ui/37_stream_pulse.ts';
import init_38_live_room from './js/ui/38_live_room.ts';
import init_39_cast_fx from './js/ui/39_cast_fx.ts';
import init_40_turn_clock from './js/ui/40_turn_clock.ts';
import init_41_holo_stage from './js/ui/41_holo_stage.ts';
import init_42_attach from './js/ui/42_attach.ts';

// 静默总闸最先装：后面每个模块 init 时都能立刻 App.onQuiet 注册自己的启停
init_00_quiet(App);
init_01_start(App);
init_02_three_scene(App);
init_03_model_load_gltf_vrm(App);
init_04_bg_load(App);
init_05_move_mode(App);
init_06_fpv_mode(App);
init_07_click_interact(App);
init_08b_expression_engine(App);
init_09_emotion_controller(App);
init_09b_motion_blender(App);
init_09c_mixamo_retarget(App);
init_09d_mixamo_library_loader(App);
init_09e_mixamo_emotion_bridge(App);
init_08_state_switch(App);
init_09_websocket(App);
init_10_tts_lipsync(App);
init_11_voice_record(App);
init_12_vad_auto(App);
init_12b_silero_vad(App);
init_13_messages(App);
init_14_toast(App);
init_15_model_ui(App);
init_16_bg_ui(App);
init_17_events(App);
// 附件在事件绑定之后挂：submitText 通过 App.takeAttachments 取待发附件
init_42_attach(App);
init_18_tts_settings(App);
init_19_boot(App);
init_20_bgm_player(App);
init_28_music_ui(App);
init_29_video_ui(App);
init_32_workspace_ui(App);
init_23_name_settings(App);
init_24_footstep_sfx(App);
init_25_character_cards(App);
init_25b_llm_providers(App);
init_26_webxr_vr(App);
init_28_codex_runner(App);
init_29_task_center(App);
init_30_task_tree(App);
init_30_task_big_screen(App);
init_31_tool_chain(App);
init_34_game_fx(App);
init_ai_autonomy(App);
// VR 射线锁定仲裁最先 init：后面每块 VR 面板都要在 init 时把命中口径注册进来
init_32_vr_ray(App);
init_32_vr_hud(App);
// VR 信息层（toast/字幕）跟在 HUD 之后：两者都包裹 enterXrMode/exitXrMode，
// 后者包在外层，进入时先显示 HUD 再显示信息层
init_32_vr_ui(App);
// VR 工具栏最后 init：它包裹手柄 select / _xrPadClick，在外层才能优先命中
// （顺序：工具栏 → vr-hud 视频面板 → 戳角色）
init_32_vr_toolbar(App);
// VR 在线音乐面板跟在工具栏之后（外层）：呼出时立在视线正前方，射线先问它
init_32_vr_music(App);
// VR 在线视频面板（搜索/热门/收藏/历史 + 语音关键词）跟在音乐面板之后（外层）
init_32_vr_video(App);
// VR 对话大屏跟在工具栏之后（外层）：它也包裹手柄 select / _xrPadClick，
// 放外层时射线先问顶栏/状态条，未命中再回落到工具栏 → vr-hud → 戳角色
init_32_vr_chat(App);
// VR 语音面板再包一层（最外层）：手柄扳机按下时先问语音面板，未命中才回落到
// 工具栏 → vr-hud → 对话大屏 → 戳角色（语音是头显里最高频的入口）
init_32_vr_voice(App);
init_33_stage_wheel(App);
init_35_arcade_fx(App);
init_36_layout_sync(App);
init_37_stream_pulse(App);
// 时钟先于直播舱：HUD 要在 init 时就能拿到 App.turnClock 注册 onChange 回调
init_40_turn_clock(App);
init_38_live_room(App);
init_39_cast_fx(App);
// 全息舞台放最后：它要装饰 App.game.onToolResult 与 App.arcade.pulse，
// 必须等这两条链路（34 / 35 / 38 / 39）都挂好，否则包在空对象上
init_41_holo_stage(App);
// VR 特效层放全息舞台之后：它要包 App.holo.* / App.game.onToolResult /
// App.arcade.pulse，必须等 41 挂好这些出口，否则包在空对象上
init_32_vr_fx(App);
// VR 全息投影 + 舞台灯光再放 vr-fx 之后：它包 vr-fx 的 celebrate/encourage 拿
// 欢呼亮度，必须等 fx 把这两个出口接到内部函数上，否则包在空函数上
init_32_vr_holo(App);

// DEBUG expose
Object.defineProperty(window, '_App', { get: () => App });
Object.defineProperty(window, '_GameManager', { get: () => window._gameManager });
Object.defineProperty(window, '_expressionRL', { get: () => App._expressionRL });
Object.defineProperty(window, '_motionLib', { get: () => App.MOTION_LIBRARY });
Object.defineProperty(window, '_mixamoLib', { get: () => App.mixamoClips });
Object.defineProperty(window, '_animLib', { get: () => ({ config: App._animLibraryConfig, stats: App.getAnimLibraryStats() }) });
