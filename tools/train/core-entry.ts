/* 训练侧打包入口：把浏览器里跑的物理与策略 IO 原样搬进 Node。
 * 训练环境和前端必须是同一份 ActiveRagdoll / policy-io 代码，否则 sim2sim 无从谈起。
 * 用法：node tools/train/build-core.mjs */
export * from '../../web/js/physics/active-ragdoll.js';
export * from '../../web/js/physics/policy-io.js';
export * from '../../web/js/physics/ragdoll-config.js';
