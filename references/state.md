# 项目状态与记录约定

## 一项事实一个来源

| 职责 | 默认权威文件 | 更新时机 | 生成视图 |
|---|---|---|---|
| 目标、预算、学习偏好、模块路线 | data/course.json | 用户调整或周期复盘 | plan.html |
| 当前进度、证据、复习队列、每日活动、周期 | data/progress.json | 小段、重要订正、收尾 | 学习上下文.md、首页、下次小纲、周期复习页 |
| 当前场次计时 | data/session.json | 每次教学回复前及暂停／结束 | 学习计时.md |
| 资料、目录、页段与页码问题 | materials/catalogue.json | 初始化、解析与核验后 | plan.html 的资料目录 |
| 题目来源与曝光 | data/questions.json | 收录、展示、提示与作答 | 首页资料状态与题库状态 |
| 当日知识与真实错题原文 | records/YYYY-MM-DD学习要点、错题总结以及注意点.html | 按保存约定 | 首页最新入口 |
| 已被替代的计划和过程证据 | archive/ | 迁移或归档 | 历史资料入口 |

生成文件注明数据来源与生成方式。直接编辑生成文件会被覆盖，更新事实应先改对应 JSON。保留用户原答案和证据出处，不能只为满足页面显示而推断事实。skill 文件及模板不存放本课程的具体学习状态。

## 数据字段

- course：schema_version、id、title、goal、timezone、target、time_policy、profile、save_policy、cycle、modules、stages、responsibilities，可选 ocr_policy（成本策略、资料范围、单批准备上限、费用上限）。这是计划偏好，不是付费执行授权。期限和目标分数是计划，不是已取得结果。模块可同时标进度与资料核验状态。
- progress：schema_version、updated_at、current（阶段、小结、未完成、下一次入口）、knowledge（id、title、evidence_history、review_status，可选 legacy_summary）、review_queue、mistakes、days、periods、next_session、maintenance。days 的 duration_seconds 允许 null，表示未知，不用预算补齐。若后来补记了可靠场次，duration_seconds 为已确认部分之和，has_unknown_duration=true 保留其他未知部分，视图两者同时展示。维护活动不进入 days。
- session：id、status（idle／running／paused／ended）、started_at、budget_seconds、deadline_at、pauses、last_checked_at、ended_at、finish_reason、notice_points_seen，可选 closing_windows、review_only、daily_budget_check。本场窗口固定在场次中，加时不重置。预算检查保存具体分钟、累计与覆盖依据；extensions 保留每次加时的检查。时间用含时区 ISO 8601；状态 ended 的核时不产生新的学习时间。闭合场次同时归档进 progress.sessions，以 id 幂等更新。
- catalogue：schema_version、sources。每个来源有 sha256、page_count、toc_pdf_pages、toc、verified_ranges、pending、ocr，可选 parsed（directory、status、manifest）。toc 条目保留 title、printed_start、pdf_start（未知为 null）、verified；目录已读与正文未读分开。
- questions：schema_version、items。id 唯一；provenance、source、answer_source、verification、knowledge_ids、exposure 必须明确。批次未知不推造。

knowledge 不再用一个自由文本状态覆盖各类证据。向 evidence_history 追加事件，字段为 id、dimension、status、date、summary、source；id 在该知识点内唯一，date 为项目时区的 YYYY-MM-DD，source 指向存在的真实记录。维度与状态：

| dimension | status |
|---|---|
| 讲解 | 已讲解 |
| 作答 | 提示后正确、独立正确、当场订正、未通过 |
| 延迟保持 | 隔日正确、未通过 |
| 迁移 | 迁移正确、未通过 |

延迟保持事件另有 baseline_id，引用同一知识点中更早学习日的基线事件；同一天复查不能标为隔日。迁移指改变输入或条件后的实际验证，可以发生在当天。页面分别取各维度按日期排序的最新事件，同日以追加顺序为准，完整事件仍可展开，不把这些维度排成单一等级。

review_status 独立记录待复查／待隔日检查／已安排／已完成，未登记可省略或为 null。“当场订正”与“待隔日检查”可同时保留。尚无证据且无历史摘要时显示未学，可保留 status="未学"；其余新结论只写事件。不能用运行脚本、计划题目或阅读资料代替作答证据。

接入旧项目时，先备份，再运行 `render.py --root . migrate-knowledge`：原 status／evidence／last_verified／source 原样移入 legacy_summary，旧状态中已明确的待查事项转入 review_status，不新增验证事件、不改学习日和场次。旧摘要继续展示，后续从原始记录逐项补全真实事件，不凭旧摘要推造基线。该迁移可重复执行。

mistakes 保留 original、correction、historical_note、latest_note、latest_date、review_status、evidence、period。旧错题的历史注记不是当前结论，较新的验证应写入 latest_note。

## 用户解析文件的导入状态

parsed.directory 默认 `materials/parsed/<资料ID>/`；status 为 awaiting_files／pending_import／imported／partial／needs_review，表示最近检查结果，不代替实际扫描。尚无文件时 manifest 为 null，不生成假导入记录。导入后在该目录保存 Agent 维护的 import-manifest.json，catalogue 只引用清单，不重复逐文件进度。

导入清单记录 source_id、source_sha256（无原 PDF 时为 null）、imported_at、origin（用户提供；工具或模型版本未知就标未知）、files（相对路径、sha256、用途）、entrypoints、chapters（标题、正文文件与标题锚点／行范围、pdf_start／pdf_end）、assets、issues。PDF 页序未知填 null，并区分书内页码；不能从文件名编号直接断定页序。文件指纹不包含 Agent 自己生成的清单，避免每次检查都误判变化。导入文件与配套素材保留原始层级，Agent 不覆盖原始结果。

接续检查用 `scan_materials.py` 比较实际文件与既有清单，包括根目录和未登记子目录；检查脚本只读。无变化不改导入时间和既有核验结论；有变化由 Agent 更新对应文件的章节／页码映射及受影响核验状态，保留用户原稿。

能用的正文先索引，缺图、缺页或不明页码写入 issues，必要时用 partial／needs_review；imported 只表示已接入，不宣称全文核验。用户无需创建元数据。读取入口和触发时机见 [materials.md](materials.md)。

## OCR 任务与缓存

实际执行时在 `materials/ocr/<任务ID>/manifest.json` 保存任务，catalogue.sources[].ocr 仅登记后端配置状态与任务清单相对路径，不手写第二份逐页进度。未执行时无需生成假任务或批量空记录。完整约定见 [materials.md](materials.md)。

任务字段：id、sources（id／sha256／目标页序）、backend、model_version、parameters、billing（计费方式／估算依据／费用上限／币种／实际费用或待确认）、authorization（覆盖的任务与费用范围）、resource（专用或共享／释放方式／实际释放结果）、status、started_at、ended_at、output_root、pages、issues。

pages 中每页记录 source_id、source_sha256、pdf_page、processing_status、output_path、output_sha256、assets、attempts、error，以及独立的 verification（pending／verified／needs_review、method、checked_at、issue）。输出使用任务目录内的 `pages/<资料ID>/pdf-0001.md` 等稳定路径；多页输出也必须能定位原页。任务状态支持 planned／sampling／running／partial／completed／failed／stopped；有失败页时用 partial 或 failed，不用 completed 掩盖缺页。processing_status=extracted 仅说明输出已保存，不代表内容正确；需要查原页的页用 needs_review 并保留输出。

跨任务复用时比对原文件指纹、后端／模型／参数和输出指纹，引用旧输出，不靠改名重跑。用户导入结果也建来源映射，无法确定页序的标待定位。正文知识核验继续写 verified_ranges，用户已学继续写 progress；不可用 OCR 页数推导学习百分比。费用或释放结果未知时保留未知。

## 每次与周期

每次开课生成 next_session 小纲，标计划日期或“下次开课时确认”；不要填不存在的起点。分段的实际结果独立保存，不把计划内容记入已学。对照资料页段标明已核验、目录定位、待正文核验。

复习队列按到期、反复错误、提示依赖和延迟保持待查排序。周期末的最后一个计划学习日，使用当天预算安排错题与混合迁移；未上课则标待安排，移到下一次。复盘日期优先取 period.planned_learning_dates 的最后一天，其次按 course.cycle.planned_weekdays 推算（0为周一），均未设置才使用 review_weekday；空计划显示待安排，不伪造出席。下次小纲与复习页显示日期和顺延提示，不自动开课。周期快照在确实执行复盘后生成，周期未结束不能写“已完成周期复盘”。错题归档不抹除最新结果；正确后逐步降低频率，保留必要的跨周期混合检查。

周期视图显示周期日期、目标、实际活动、可靠时长与未知时长天数、最新错题状态、尚未完成、复习计划和下周期调整。实际错题每天更新，不等周期结束才保存。脚本可汇总事实，教学取舍由 Agent 根据证据判断。

实际完成复盘后给对应 period 写入 review_completed_at 和 review_evidence（项目内真实复盘记录路径），运行 `render.py --root . snapshot-period --period <周期ID>`。脚本在 records/periods 保存独立JSON与HTML快照，拒绝覆盖已有快照；未完成复盘时拒绝生成完成快照。snapshot.generated_at 为真实生成时间，state_basis="generation_time"：保存生成时看到的最新验证状态，包括周期结束后的更新。学习日仅含本周期，错题含周期结束前发生的历史条目，并区分本周期新增与带入历史；不是“截至周期末的结论”或“本周新增错题全集”。

## 每日 HTML

在课程约定要求或用户明确要求保存时创建。文件名、title、h1 统一为 `YYYY-MM-DD学习要点、错题总结以及注意点`，使用对应学习日期与项目时区。修改已有记录直接更新原文件，保留正确内容和历史原答。

使用 assets/daily-record.html；内容按实际发生填写，可省略没有内容的章节。包括实际用时、知识连接、关键适用条件、真实错题原答／订正／原因／最新证据、记忆办法和未作答复习题。补充解释、未答题和模板示例不能算新增作答。编码题说明位宽和编码类型。红色突出规则与订正，同时保留文字说明。

左侧目录链接到主章节和适合导航的小节，桌面粘附、窄屏置顶、打印隐藏；答案用 details 折叠；单文件无外部依赖，手机与打印可读，HTML／CSS／JS 无源码注释。

## 生成和检查

运行 `python3 <skill>/scripts/render.py --root . build` 刷新所有视图；运行 `python3 <skill>/scripts/validate.py --root .` 检查数据、验证日期、链接、锚点、时间、题目状态和生成文件新旧。原解析稿的辅助断链以 warnings 和 material_issues 报告，必要图片缺失仍为错误；核心页面与记录断链始终为错误。维护迁移可用 `--strict-materials` 将全部原稿断链提升为错误。发现影响题干、公式、图表或答案依据的问题，Agent 将受影响范围标 needs_review，暂不用于判题。`--quick` 跳过全文指纹和原解析目录，并在报告标出未检查范围。计时脚本自动同步生成视图，课堂状态直接取自session而不手写另一份；较长的每日HTML整理安排在课后。

点图根据 days 的真实学习日聚合，已知时长着色，未知时长使用独立样式与文字。未记录日期写“无学习记录”，不能断言用户没学。点图与知识点状态分开显示。首页提供配置与原文件链接，Markdown 以可展开正文显示；PDF 直接阅读。

## 清理与迁移

先清点文件、保存外部临时备份和内容指纹，确定调用关系，再移动资料与记录。删除纯生成重复视图可重建；真实作答、独立每日记录和资料原件保留。过时方案移入 archive 并加“历史资料，非当前安排”说明。迁移后检查所有本地链接、每日记录文本和 PDF 指纹，不把旧文件中的过期待答项带回当前状态。
