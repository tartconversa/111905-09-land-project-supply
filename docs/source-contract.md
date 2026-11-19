# 来源资料约定

宗地用途权益、开发分期、建设兑现和住房许可范围。

area_sqm为平方米十进制字符串；capacity_area、saleable_area、delivered_home_area为不同指标，不能自动互换。amount_cents是人民币分。

rights记录宗地在用途与有效期内的开发额度，amend引用原权益并用新的total_capacity明确替代总额。right_transfers引用原right_id，from_holder/to_holder为转出转入方，scope_id及share限定转让范围，转让不新增额度；valid_from/valid_to限定持有期。

scope_id是来源对物理范围的声明，coverage是独立范围原子的列表，每个原子含atom及fraction。相同atom重复主张属于覆盖重叠；份额在各原子内计算，不跨原子平均。

phase_claim只表示项目对权益范围的申报，buildings记录实际楼栋兑现的范围及用途。permits说明销售范围，completion是实际交付，不根据有许可证推断已竣工。assignment的生效期限定楼栋到分期的归属。

## 数据载体

每类资料位于 data 中同名的 NDJSON 文件，一行一个原始对象；字段和基本类型见 data/schema.json。data/manifest.json 固定资料行数与字节哈希。资料中的缺失、歧义、未完整交付及冲突声明属于来源事实，不由资料检查程序推断业务结论。

### rights

土地用途开发权益。字段：right_id (string)、parcel (string)、holder (string)、use (string)、total_capacity (string)、effective (string)、kind (string)、reference (any)。

### scopes

来源物理范围。字段：scope_id (string)、coverage (array)。

### phase_claims

分期权益占用申报。字段：claim_id (string)、phase (string)、right_id (string)、scope_id (string)、capacity_area (string)、effective (string)。

### buildings

实际建设及计量。字段：building (string)、scope_id (string)、use (string)、capacity_area (string)、saleable_area (string)、delivered_home_area (string)、completion (any)。

### assignments

楼栋分期归属。字段：record_id (string)、building (string)、phase (string)、valid_from (string)、valid_to (any)、received (string)。

### permits

住房销售许可范围。字段：permit_id (string)、scope_id (string)、building (string)、use (string)、saleable_area (string)、valid_from (string)。

### land_consideration

宗地对价事实。字段：payment_id (string)、parcel (string)、amount_cents (integer)、date (string)。

### right_transfers

联合开发部分权益转让。字段：transfer_id (string)、right_id (string)、from_holder (string)、to_holder (string)、scope_id (string)、share (string)、valid_from (string)、valid_to (any)、received (string)。
