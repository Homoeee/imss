"""匿名订单正文回归：验证字段提取及不能自动放行的歧义情况。"""
import unittest

from imss.core.models import Material
from imss.recognition.fields import extract_amount, extract_order_ids
from imss.recognition.rules import recognize


def read(text):
    return recognize(Material('anonymous.png', 'sample', '.png', text=text))


class OrderFieldTests(unittest.TestCase):
    def test_title_without_label_uses_product_card(self):
        material = read('交易成功\n文具专营店\n进店逛逛\n加厚活动用纸 ¥35.8\n白色 A4 ¥40\nx1\n实付款 共减4.2 ¥35.8\n订单信息\n1234567890123456 复制')
        self.assertEqual((material.kind, material.item, material.amount), ('购买记录', '加厚活动用纸', '35.80'))
        self.assertEqual(material.order_ids, ['1234567890123456'])
        self.assertFalse(material.issues)

    def test_explicit_name_accepts_no_colon_and_next_line(self):
        for name in ('商品名称 活动用纸', '商品名称\n活动用纸', '商品说明：活动用纸'):
            with self.subTest(name=name):
                self.assertEqual(read('交易成功\n' + name + '\n实付款 ¥35.80').item, '活动用纸')

    def test_title_and_price_on_separate_lines(self):
        material = read('交易成功\n加厚活动用纸\n¥35.80\nx1\n实付款 ¥35.80')
        self.assertEqual(material.item, '加厚活动用纸')

    def test_adjacent_specification_is_not_second_product(self):
        material = read('交易成功\n毕业拍照手举牌 ¥30.48\n【主图款】蓝色手举牌8个 ¥35.88\nx1\n实付款 ¥30.48')
        self.assertEqual(material.item, '毕业拍照手举牌')
        self.assertFalse(material.issues)

    def test_multiple_product_cards_require_review(self):
        material = read('交易成功\n加厚活动用纸 ¥20\nx1\n彩色记号笔 ¥10\nx2\n实付款 ¥40')
        self.assertFalse(material.item)
        self.assertTrue(any('多个商品' in issue for issue in material.issues))

    def test_specification_without_title_stays_blank(self):
        for specification in ('米白色 3cm;38 ¥149', '该规格拼单价 ¥12.9', '颜色随机 [实际发11个] ¥15'):
            with self.subTest(specification=specification):
                self.assertFalse(read('交易成功\n' + specification + '\nx1\n实付款 ¥126').item)

    def test_recommended_product_is_not_ordered_product(self):
        material = read('交易成功\n加厚活动用纸 ¥20\nx1\n实付款 ¥20\n猜你喜欢\n彩色记号笔 ¥10\nx1')
        self.assertEqual(material.item, '加厚活动用纸')

    def test_shop_and_service_are_not_titles(self):
        material = read('交易成功\n文具专营店 ¥100\n退货运费保障 ¥5\nx1\n实付款 ¥20')
        self.assertFalse(material.item)

    def test_invoice_table_header_is_not_item(self):
        material = read('电子发票\n项目名称 规格型号 单位 数量 单价 金额 税率 税额\n价税合计(小写)¥29.15')
        self.assertFalse(material.item)
        self.assertEqual(material.amount, '29.15')

    def test_paid_beats_original_price_and_discount(self):
        material = read('交易成功\n商品名称：活动用纸\n商品总价 ¥40\n优惠金额 ¥4.2\n实付款 共减4.2 ¥35.8')
        self.assertEqual(material.amount, '35.80')
        self.assertFalse(material.issues)

    def test_all_actual_payment_labels_are_compared(self):
        for labels in ('实付款 ¥35.8\n实际支付 ¥20', '实付金额 ¥35.8 付款金额 ¥20'):
            with self.subTest(labels=labels):
                material = read('交易成功\n商品名称：活动用纸\n' + labels)
                self.assertFalse(material.amount)
                self.assertTrue(any('冲突' in issue for issue in material.issues))

    def test_repeated_payment_value_is_not_conflict(self):
        material = read('交易成功\n商品名称：活动用纸\n实付款 ¥35.8\n实际支付 ¥35.80')
        self.assertEqual(material.amount, '35.80')
        self.assertFalse(material.issues)

    def test_two_unlabelled_currency_values_are_not_discount_guess(self):
        material = read('交易成功\n商品名称：活动用纸\n实付款 HG ¥2.3 ¥12.7')
        self.assertFalse(material.amount)
        self.assertTrue(any('冲突' in issue for issue in material.issues))

    def test_discount_in_paid_field_is_excluded(self):
        material = read('交易成功\n商品名称：活动用纸\n实付款 优惠¥2.3 ¥12.7')
        self.assertEqual(material.amount, '12.70')
        self.assertFalse(material.issues)

    def test_paid_field_next_line_or_before_label(self):
        for layout in ('实付款\n¥35.80', '¥35.80 实付款', '¥35.80\n实付款', '实付款 35.80元'):
            with self.subTest(layout=layout):
                self.assertEqual(read('交易成功\n商品名称：活动用纸\n' + layout).amount, '35.80')

    def test_unreadable_actual_payment_does_not_fall_back_to_total(self):
        material = read('交易成功\n商品名称：活动用纸\n订单总额 ¥40\n实付款\n运费 ¥5')
        self.assertFalse(material.amount)
        self.assertTrue(material.issues)

    def test_paid_label_does_not_cross_inline_shipping_field(self):
        material = read('交易成功\n商品名称：活动用纸\n实付款 运费\n¥5')
        self.assertFalse(material.amount)

    def test_one_unreadable_payment_label_keeps_warning(self):
        material = read('交易成功\n商品名称：活动用纸\n实付款 ¥35.80\n实际支付 ???')
        self.assertEqual(material.amount, '35.80')
        self.assertTrue(material.issues)

    def test_totals_remain_unconfirmed(self):
        material = read('交易成功\n商品名称：活动用纸\n应付金额 ¥35.80')
        self.assertEqual(material.amount, '35.80')
        self.assertTrue(any('实际支付' in issue for issue in material.issues))

    def test_broken_decimal_is_proposed_with_warning(self):
        material = read('交易成功\n商品名称：活动用纸\n实付款 共减22.26 ¥49 34 ~')
        self.assertEqual(material.amount, '49.34')
        self.assertTrue(any('小数点' in issue for issue in material.issues))

    def test_ocr_y_currency_and_spacing(self):
        material = read('交易 成 功\n商品 名称：活动 用纸\n实 付款 He Y22.5~')
        self.assertEqual(material.amount, '22.50')
        self.assertFalse(material.issues)

    def test_extra_unexplained_digits_remain_flagged(self):
        material = read('交易成功\n商品名称：活动用纸\n实付款 ¥11.88 YD7.12')
        self.assertTrue(any('未解释' in issue for issue in material.issues))

    def test_invalid_money_never_truncates_to_valid_prefix(self):
        for value in ('¥12.345', '¥12.7%', '¥1,23', '¥0', '-¥3', '2026-05-01', '12:30:00'):
            with self.subTest(value=value):
                material = read('交易成功\n商品名称：活动用纸\n实付款 ' + value)
                self.assertFalse(material.amount)
                self.assertTrue(material.issues)

    def test_refund_and_unpaid_orders_keep_warning(self):
        for status in ('已退款', '退款成功', '部分退款', '订单状态：待付款', '交易关闭'):
            with self.subTest(status=status):
                material = read('订单编号 12345678\n商品名称：活动用纸\n' + status + '\n实付款 ¥35.80')
                self.assertTrue(material.issues)

    def test_service_refund_promise_is_not_actual_refund(self):
        material = read('交易成功\n商品名称：活动用纸\n极速退款 退货运费保障\n实付款 ¥35.80')
        self.assertFalse(material.issues)

    def test_successful_payment_bill_reads_top_expense(self):
        for header, state, amount in (('账单详情', '自动扣款成功', '19.20'), ('交易详情', '当前状态 支付成功', '-22.63')):
            with self.subTest(header=header):
                material = read(header + '\n' + amount + '\n' + state + '\n支付时间 2026年05月01日 12:30:00\n商品说明：高德打车订单')
                self.assertEqual(material.amount, amount.lstrip('-'))
                self.assertFalse(material.issues)

    def test_bill_conflicting_expenses_stay_blank(self):
        material = read('账单详情\n-19.20\n-22.63\n支付成功\n商品说明：活动用纸')
        self.assertFalse(material.amount)

    def test_bill_refund_does_not_supply_top_expense(self):
        material = read('账单详情\n-19.20\n支付成功\n已退款\n商品说明：活动用纸')
        self.assertFalse(material.amount)

    def test_trip_amount_requires_manual_payment_check(self):
        material = read('行程已结束\n¥19.80\n快车全程14.73公里 22分钟')
        self.assertEqual(material.amount, '19.80')
        self.assertTrue(any('实际支付' in issue for issue in material.issues))

    def test_ids_support_underscores_quotes_and_line_breaks(self):
        self.assertEqual(extract_order_ids('商户单号 “212_2026010112345678\n交易单号\n42000001234567890123456789\n订单编号 1234 5678 9012 复制'),
                         ['123456789012', '212_2026010112345678', '42000001234567890123456789'])

    def test_unlabelled_phone_date_or_words_are_not_order_id(self):
        self.assertFalse(extract_order_ids('联系电话 13800000000\n2026-05-01\n订单号：2026-05-01\n订单号：undefined'))

    def test_twenty_digit_transaction_is_not_invoice_id(self):
        material = read('交易成功\n交易单号 12345678901234567890\n商品名称：活动用纸\n实付款 ¥35.80')
        self.assertEqual(material.order_ids, ['12345678901234567890'])
        self.assertFalse(material.invoice_ids)

    def test_invoice_link_does_not_turn_order_into_invoice(self):
        for marker in ('交易成功', '订单详情', '账单详情', '实付金额 ¥35.80'):
            with self.subTest(marker=marker):
                material = read(marker + '\n查看电子发票\n发票号码：12345678901234567890\n商品名称：活动用纸')
                self.assertEqual(material.kind, '购买记录')
                self.assertEqual(material.invoice_ids, ['12345678901234567890'])

    def test_invoice_still_uses_gross_total(self):
        self.assertEqual(extract_amount('金额 ¥31.68 税额 ¥4.12\n价税合计(大写)叁拾伍元捌角(小写)￥35.80', '电子发票'), ('35.80', []))


if __name__ == '__main__':
    unittest.main()
