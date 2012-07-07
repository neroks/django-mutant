
from django.db.models import fields
from django.utils.translation import ugettext_lazy as _

from ...models import FieldDefinition


auto_now_help_text = _(u'Automatically set the field to now every time the '
                       u'object is saved.')

auto_now_add_help_text = _(u'Automatically set the field to now when the '
                           u'object is first created.')

class DateFieldDefinition(FieldDefinition):
    
    auto_now = fields.BooleanField(_(u'auto now'), default=False,
                                   help_text=auto_now_help_text)
    
    auto_now_add = fields.BooleanField(_(u'auto now add'), default=False,
                                       help_text=auto_now_add_help_text)
    
    class Meta:
        app_label = 'mutant'
        defined_field_class = fields.DateField
        defined_field_options = ('auto_now', 'auto_now_add',)
        defined_field_category = _(u'Temporal')

class TimeFieldDefinition(DateFieldDefinition):
    
    class Meta:
        app_label = 'mutant'
        proxy = True
        defined_field_class = fields.TimeField

class DateTimeFieldDefinition(DateFieldDefinition):
    
    class Meta:
        app_label = 'mutant'
        proxy = True
        defined_field_class = fields.DateTimeField