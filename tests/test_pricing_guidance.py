import copy
import unittest
from unittest.mock import patch
from bot import content_store as store, config, pricing_guidance as guide
from bot.calculator import calculate

class GuidanceTests(unittest.IsolatedAsyncioTestCase):
    def test_percent_conversion(self):
        self.assertEqual(guide.percent_to_stored('coef','urgent',25),1.25)
        self.assertEqual(guide.percent_to_stored('round','price_from_factor',10),.9)
        self.assertEqual(guide.percent_to_stored('round','price_to_factor',10),1.1)
        self.assertEqual(guide.percent_to_stored('round','round_to',100),100)
        for kind,key,value in [('round','price_from_factor',100),('coef','urgent',float('nan')),('round','round_to',0),('round','round_to',1.5)]:
            with self.assertRaises(ValueError): guide.percent_to_stored(kind,key,value)

    async def test_simple_mode_keeps_live_prices_and_calculates_50000_plus_options(self):
        data={'services':[{'id':'S','name':'Лендинг','base_price':50000,'term_min':10,'term_max':14}],
              'options':[{'id':'O','service_id':'S','name':'Экран','price':5000,'days':1,'multipliable':True}],
              'coefficients':{'urgent':{'multiplier':1.3},'complex':{'multiplier':1.5}},
              'rounding':{'price_from_factor':.9,'price_to_factor':1.1,'round_to':1000}}
        original=copy.deepcopy(data)
        async def read(_): return copy.deepcopy(data)
        async def write(_,new): data.clear();data.update(copy.deepcopy(new))
        with patch.object(store,'_read',side_effect=read),patch.object(store,'_write',side_effect=write),patch.object(store,'_upstash_enabled',return_value=False):
            await store.stage_simple_pricing(config.DESIGNER_CHAT_ID)
            self.assertEqual(store._without_editor(data),original)
            draft=await store.get_pricing_edit(config.DESIGNER_CHAT_ID)
            self.assertEqual(calculate(draft,'S',[],False,False).price_from,50000)
            self.assertEqual(calculate(draft,'S',[{'id':'O','qty':2}],True,False).price_to,60000)
            with self.assertRaises(store.NotDesignerError): await store.stage_simple_pricing('other')
