from __future__ import unicode_literals

from functools import wraps

from django.contrib.contenttypes.models import ContentType
from django.db import connections, models, transaction
from django.db.models.fields import FieldDoesNotExist
from django.db.models.signals import (
    m2m_changed, post_delete, post_save, pre_delete,
)
from django.dispatch.dispatcher import receiver

from ..models import (
    BaseDefinition, FieldDefinition, ModelDefinition, UniqueTogetherDefinition,
)
from ..state import handler as state_handler
from ..utils import allow_migrate, popattr, remove_from_app_cache


def perform_ddl(action, model, *args, **kwargs):
    if model._meta.managed:
        return

    for alias in allow_migrate(model):
        connection = connections[alias]
        with transaction.atomic(alias), connection.schema_editor() as editor:
            getattr(editor, action)(model, *args, **kwargs)


def nonraw_instance(receiver):
    """
    A signal receiver decorator that fetch the complete instance from db when
    it's passed as raw
    """
    @wraps(receiver)
    def wrapper(sender, instance, raw, using, **kwargs):
        if raw:
            instance = sender._default_manager.using(using).get(pk=instance.pk)
        return receiver(sender=sender, raw=raw, instance=instance, using=using,
                        **kwargs)
    return wrapper


@receiver(post_save, sender=ModelDefinition,
          dispatch_uid='mutant.management.model_definition_post_save')
@nonraw_instance
def model_definition_post_save(sender, instance, created, **kwargs):
    model_class = instance.model_class(force_create=True)
    opts = model_class._meta
    db_table = opts.db_table
    if created:
        primary_key = opts.pk
        fields = [(field.get_attname_column()[1], field) for field in opts.fields
                  if field is not primary_key]
        try:
            extra_fields = getattr(instance._state, '_create_extra_fields')
        except AttributeError:
            pass
        else:
            for column, field in extra_fields:
                if field.primary_key:
                    assert isinstance(primary_key, models.AutoField)
                    primary_key = field
                elif (field.rel and field.rel.parent_link and
                      isinstance(primary_key, models.AutoField)):
                    field.primary_key = True
                    primary_key = field
                else:
                    fields.append((column, field))
            delattr(instance._state, '_create_extra_fields')
        fields.insert(0, (primary_key.get_attname_column()[1], primary_key))
        try:
            delayed_save = getattr(instance._state, '_create_delayed_save')
        except AttributeError:
            pass
        else:
            for obj in delayed_save:
                obj.model_def = instance
                obj.save(force_insert=True, force_create_model_class=False)
            delattr(instance._state, '_create_delayed_save')
        model_class = instance.model_class(force_create=True)
        perform_ddl('create_model', model_class)
    else:
        old_model_class = instance._model_class
        if old_model_class:
            old_db_table = old_model_class._meta.db_table
            if db_table != old_db_table:
                perform_ddl('alter_db_table', model_class, old_db_table, db_table)
            remove_from_app_cache(old_model_class)
            ContentType.objects.clear_cache()
    instance._model_class = model_class.model


@receiver(pre_delete, sender=ModelDefinition,
          dispatch_uid='mutant.management.model_definition_pre_delete')
def model_definition_pre_delete(sender, instance, **kwargs):
    model_class = instance.model_class()
    instance._state._deletion = (
        model_class,
        instance.pk,
    )


@receiver(post_delete, sender=ModelDefinition,
          dispatch_uid='mutant.management.model_definition_post_delete')
def model_definition_post_delete(sender, instance, **kwargs):
    model_class, pk = popattr(instance._state, '_deletion')
    perform_ddl('delete_model', model_class)
    remove_from_app_cache(model_class)
    model_class.mark_as_obsolete()
    state_handler.clear_checksum(pk)
    ContentType.objects.clear_cache()
    del instance._model_class


@receiver(post_save, sender=BaseDefinition,
          dispatch_uid='mutant.management.base_definition_post_save')
def base_definition_post_save(sender, instance, created, raw, **kwargs):
    declared_fields = instance.get_declared_fields()
    if declared_fields:
        model_class = instance.model_def.model_class()
        opts = model_class._meta
        if created:
            add_columns = popattr(instance._state, '_add_columns', True)
            if add_columns:
                auto_pk = isinstance(opts.pk, models.AutoField)
                for field in declared_fields:
                    if auto_pk and field.rel and field.rel.parent_link:
                        auto_pk = False
                        field.primary_key = True
                        perform_ddl('alter_field', model_class, opts.pk, field, strict=True)
                    else:
                        perform_ddl('add_field', model_class, field)
        else:
            for field in declared_fields:
                try:
                    old_field = opts.get_field(field.name)
                except FieldDoesNotExist:
                    perform_ddl('add_field', model_class, field)
                else:
                    perform_ddl('alter_field', model_class, old_field, field, strict=True)


@receiver(pre_delete, sender=BaseDefinition,
          dispatch_uid='mutant.management.base_definition_pre_delete')
def base_definition_pre_delete(sender, instance, **kwargs):
    """
    This is used to pass data required for deletion to the post_delete
    signal that is no more available thereafter.
    """
    # see CASCADE_MARK_ORIGIN's docstring
    cascade_deletion_origin = popattr(
        instance._state, '_cascade_deletion_origin', None
    )
    if cascade_deletion_origin == 'model_def':
        return
    if (instance.base and issubclass(instance.base, models.Model) and
            instance.base._meta.abstract):
        instance._state._deletion = instance.model_def.model_class()


@receiver(post_delete, sender=BaseDefinition,
          dispatch_uid='mutant.management.base_definition_post_delete')
def base_definition_post_delete(sender, instance, **kwargs):
    """
    Make sure to delete fields inherited from an abstract model base.
    """
    if hasattr(instance._state, '_deletion'):
        model = popattr(instance._state, '_deletion')
        for field in instance.base._meta.fields:
            perform_ddl('remove_field', model, field)


@receiver(m2m_changed, sender=UniqueTogetherDefinition.field_defs.through,
          dispatch_uid='mutant.management.unique_together_field_defs_changed')
def unique_together_field_defs_changed(instance, action, model, **kwargs):
    model_class = instance.model_def.model_class()
    if action.startswith('post_'):
        perform_ddl(
            'alter_unique_together',
            model_class,
            model_class._meta.unique_together,
            instance.model_def.get_state().options.get('unique_together', [])
        )
        model_class.mark_as_obsolete()


@receiver(post_save, sender=FieldDefinition,
          dispatch_uid='mutant.management.raw_field_definition_proxy_post_save')
def raw_field_definition_proxy_post_save(sender, instance, raw, **kwargs):
    """
    When proxy field definitions are loaded from a fixture they're not
    passing through the `field_definition_post_save` signal. Make sure they
    are.
    """
    if raw:
        model_class = instance.content_type.model_class()
        opts = model_class._meta
        if opts.proxy and opts.concrete_model is sender:
            field_definition_post_save(
                sender=model_class, instance=instance.type_cast(), raw=raw,
                **kwargs
            )


@nonraw_instance
def field_definition_post_save(sender, instance, created, raw, **kwargs):
    """
    This signal is connected by all FieldDefinition subclasses
    see comment in FieldDefinitionBase for more details
    """
    model_class = instance.model_def.model_class()
    field = instance._south_ready_field_instance()
    field.model = model_class
    if created:
        if hasattr(instance._state, '_creation_default_value'):
            field.default = instance._state._creation_default_value
            delattr(instance._state, '_creation_default_value')
        add_column = popattr(instance._state, '_add_column', True)
        if add_column:
            perform_ddl('add_field', model_class, field)
    else:
        old_field = instance._state._pre_save_field
        delattr(instance._state, '_pre_save_field')
        perform_ddl('alter_field', model_class, old_field, field, strict=True)

FIELD_DEFINITION_POST_SAVE_UID = "mutant.management.%s_post_save"


@receiver(pre_delete, sender=FieldDefinition,
          dispatch_uid='mutant.management.field_definition_pre_delete')
def field_definition_pre_delete(sender, instance, **kwargs):
    # see CASCADE_MARK_ORIGIN's docstring
    cascade_deletion_origin = popattr(
        instance._state, '_cascade_deletion_origin', None
    )
    if cascade_deletion_origin == 'model_def':
        return
    model_class = instance.model_def.model_class()
    opts = model_class._meta
    field = opts.get_field(instance.name)
    instance._state._deletion = (model_class, field)


@receiver(post_delete, sender=FieldDefinition,
          dispatch_uid='mutant.management.field_definition_post_delete')
def field_definition_post_delete(sender, instance, **kwargs):
    if hasattr(instance._state, '_deletion'):
        model, field = popattr(instance._state, '_deletion')
        if field.primary_key:
            primary_key = models.AutoField(name='id', primary_key=True)
            perform_ddl('alter_field', model, field, primary_key, strict=True)
        else:
            perform_ddl('remove_field', model, field)
