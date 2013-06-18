import logging
import time
from datetime import datetime, timedelta
from urlparse import urlparse

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.core.urlresolvers import resolve
from django.db import models, IntegrityError
from django.db.models import Q
from django.http import Http404

from pyquery import PyQuery
from statsd import statsd
from tidings.models import NotificationsMixin
from tower import ugettext_lazy as _lazy, ugettext as _

from kitsune.products.models import Product, Topic as NewTopic
from kitsune.questions.models import Question
from kitsune.search.es_utils import UnindexMeBro, ES_EXCEPTIONS
from kitsune.search.models import (
    SearchMappingType, SearchMixin, register_for_indexing,
    register_mapping_type)
from kitsune.sumo import ProgrammingError
from kitsune.sumo.models import ModelBase, LocaleField
from kitsune.sumo.urlresolvers import reverse, split_path
from kitsune.tags.models import BigVocabTaggableMixin
from kitsune.topics.models import Topic
from kitsune.wiki import TEMPLATE_TITLE_PREFIX
from kitsune.wiki.config import (
    CATEGORIES, SIGNIFICANCES, TYPO_SIGNIFICANCE, MEDIUM_SIGNIFICANCE,
    MAJOR_SIGNIFICANCE, REDIRECT_HTML, REDIRECT_CONTENT, REDIRECT_TITLE,
    REDIRECT_SLUG)
from kitsune.wiki.permissions import DocumentPermissionMixin


log = logging.getLogger('k.wiki')


class TitleCollision(Exception):
    """An attempt to create two pages of the same title in one locale"""


class SlugCollision(Exception):
    """An attempt to create two pages of the same slug in one locale"""


class _NotDocumentView(Exception):
    """A URL not pointing to the document view was passed to from_url()."""


class Document(NotificationsMixin, ModelBase, BigVocabTaggableMixin,
               SearchMixin, DocumentPermissionMixin):
    """A localized knowledgebase document, not revision-specific."""
    title = models.CharField(max_length=255, db_index=True)
    slug = models.CharField(max_length=255, db_index=True)

    # Is this document a template or not?
    is_template = models.BooleanField(default=False, editable=False,
                                      db_index=True)
    # Is this document localizable or not?
    is_localizable = models.BooleanField(default=True, db_index=True)

    # TODO: validate (against settings.SUMO_LANGUAGES?)
    locale = LocaleField(default=settings.WIKI_DEFAULT_LANGUAGE, db_index=True)

    # Latest approved revision. L10n dashboard depends on this being so (rather
    # than being able to set it to earlier approved revisions). (Remove "+" to
    # enable reverse link.)
    current_revision = models.ForeignKey('Revision', null=True,
                                         related_name='current_for+')

    # Latest revision which both is_approved and is_ready_for_localization,
    # This may remain non-NULL even if is_localizable is changed to false.
    latest_localizable_revision = models.ForeignKey(
        'Revision', null=True, related_name='localizable_for+')

    # The Document I was translated from. NULL iff this doc is in the default
    # locale or it is nonlocalizable. TODO: validate against
    # settings.WIKI_DEFAULT_LANGUAGE.
    parent = models.ForeignKey('self', related_name='translations',
                               null=True, blank=True)

    # Cached HTML rendering of approved revision's wiki markup:
    html = models.TextField(editable=False)

    # A document's category must always be that of its parent. If it has no
    # parent, it can do what it wants. This invariant is enforced in save().
    category = models.IntegerField(choices=CATEGORIES, db_index=True)

    # A document's is_archived flag must match that of its parent. If it has no
    # parent, it can do what it wants. This invariant is enforced in save().
    is_archived = models.BooleanField(
        default=False, db_index=True, verbose_name='is obsolete',
        help_text=_lazy(
            u'If checked, this wiki page will be hidden from basic searches '
             'and dashboards. When viewed, the page will warn that it is no '
             'longer maintained.'))

    # Enable discussion (kbforum) on this document.
    allow_discussion = models.BooleanField(
        default=True, help_text=_lazy(
            u'If checked, this document allows discussion in an associated '
             'forum. Uncheck to hide/disable the forum.'))

    # List of users that have contributed to this document.
    contributors = models.ManyToManyField(User)

    # List of products this document applies to.
    products = models.ManyToManyField(Product)

    # List of topics this document applies to.
    topics = models.ManyToManyField(Topic)

    # List of product-specific topics this document applies to.
    # TODO: Remove old topics above and rename this to topics.
    # We'll have to pass a db_table param to specify the table name.
    new_topics = models.ManyToManyField(NewTopic)

    # Needs change fields.
    needs_change = models.BooleanField(default=False, help_text=_lazy(
        u'If checked, this document needs updates.'), db_index=True)
    needs_change_comment = models.CharField(max_length=500, blank=True)

    # firefox_versions,
    # operating_systems:
    #    defined in the respective classes below. Use them as in
    #    test_firefox_versions.

    # TODO: Rethink indexes once controller code is near complete. Depending on
    # how MySQL uses indexes, we probably don't need individual indexes on
    # title and locale as well as a combined (title, locale) one.
    class Meta(object):
        unique_together = (('parent', 'locale'), ('title', 'locale'),
                           ('slug', 'locale'))
        permissions = [('archive_document', 'Can archive document'),
                       ('edit_needs_change', 'Can edit needs_change')]

    def _collides(self, attr, value):
        """Return whether there exists a doc in this locale whose `attr` attr
        is equal to mine."""
        return Document.uncached.filter(locale=self.locale,
                                        **{attr: value}).exists()

    def _raise_if_collides(self, attr, exception):
        """Raise an exception if a page of this title/slug already exists."""
        if self.id is None or hasattr(self, 'old_' + attr):
            # If I am new or my title/slug changed...
            if self._collides(attr, getattr(self, attr)):
                raise exception

    def clean(self):
        """Translations can't be localizable."""
        self._clean_is_localizable()
        self._clean_category()
        self._ensure_inherited_attr('is_archived')

    def _clean_is_localizable(self):
        """is_localizable == allowed to have translations. Make sure that isn't
        violated.

        For default language (en-US), is_localizable means it can have
        translations. Enforce:
            * is_localizable=True if it has translations
            * if has translations, unable to make is_localizable=False

        For non-default langauges, is_localizable must be False.

        """
        if self.locale != settings.WIKI_DEFAULT_LANGUAGE:
            self.is_localizable = False

        # Can't save this translation if parent not localizable
        if self.parent and not self.parent.is_localizable:
            raise ValidationError('"%s": parent "%s" is not localizable.' % (
                                  unicode(self), unicode(self.parent)))

        # Can't make not localizable if it has translations
        # This only applies to documents that already exist, hence self.pk
        # TODO: Use uncached manager here, if we notice problems
        if self.pk and not self.is_localizable and self.translations.exists():
            raise ValidationError('"%s": document has %s translations but is '
                                  'not localizable.' % (
                                  unicode(self), self.translations.count()))

    def _ensure_inherited_attr(self, attr):
        """Make sure my `attr` attr is the same as my parent's if I have one.

        Otherwise, if I have children, make sure their `attr` attr is the same
        as mine.

        """
        if self.parent:
            # We always set the child according to the parent rather than vice
            # versa, because we do not expose an Archived checkbox in the
            # translation UI.
            setattr(self, attr, getattr(self.parent, attr))
        else:  # An article cannot have both a parent and children.
            # Make my children the same as me:
            if self.id:
                self.translations.all().update(**{attr: getattr(self, attr)})

    def _clean_category(self):
        """Make sure a doc's category is the same as its parent's."""
        if (not self.parent and
            self.category not in (id for id, name in CATEGORIES)):
            # All we really need to do here is make sure category != '' (which
            # is what it is when it's missing from the DocumentForm). The extra
            # validation is just a nicety.
            raise ValidationError(_('Please choose a category.'))
        self._ensure_inherited_attr('category')

    def _attr_for_redirect(self, attr, template):
        """Return the slug or title for a new redirect.

        `template` is a Python string template with "old" and "number" tokens
        used to create the variant.

        """
        def unique_attr():
            """Return a variant of getattr(self, attr) such that there is no
            Document of my locale with string attribute `attr` equal to it.

            Never returns the original attr value.

            """
            # "My God, it's full of race conditions!"
            i = 1
            while True:
                new_value = template % dict(old=getattr(self, attr), number=i)
                if not self._collides(attr, new_value):
                    return new_value
                i += 1

        old_attr = 'old_' + attr
        if hasattr(self, old_attr):
            # My slug (or title) is changing; we can reuse it for the redirect.
            return getattr(self, old_attr)
        else:
            # Come up with a unique slug (or title):
            return unique_attr()

    def save(self, *args, **kwargs):
        self.is_template = self.title.startswith(TEMPLATE_TITLE_PREFIX)

        self._raise_if_collides('slug', SlugCollision)
        self._raise_if_collides('title', TitleCollision)

        # These are too important to leave to a (possibly omitted) is_valid
        # call:
        self._clean_is_localizable()
        self._ensure_inherited_attr('is_archived')
        # Everything is validated before save() is called, so the only thing
        # that could cause save() to exit prematurely would be an exception,
        # which would cause a rollback, which would negate any category changes
        # we make here, so don't worry:
        self._clean_category()

        super(Document, self).save(*args, **kwargs)

        # Make redirects if there's an approved revision and title or slug
        # changed. Allowing redirects for unapproved docs would (1) be of
        # limited use and (2) require making Revision.creator nullable.
        slug_changed = hasattr(self, 'old_slug')
        title_changed = hasattr(self, 'old_title')
        if self.current_revision and (slug_changed or title_changed):
            doc = Document.objects.create(locale=self.locale,
                                          title=self._attr_for_redirect(
                                              'title', REDIRECT_TITLE),
                                          slug=self._attr_for_redirect(
                                              'slug', REDIRECT_SLUG),
                                          category=self.category,
                                          is_localizable=False)
            Revision.objects.create(document=doc,
                                    content=REDIRECT_CONTENT % self.title,
                                    is_approved=True,
                                    reviewer=self.current_revision.creator,
                                    creator=self.current_revision.creator)

            if slug_changed:
                del self.old_slug
            if title_changed:
                del self.old_title

        self.parse_and_calculate_links()

    def __setattr__(self, name, value):
        """Trap setting slug and title, recording initial value."""
        # Public API: delete the old_title or old_slug attrs after changing
        # title or slug (respectively) to suppress redirect generation.
        if getattr(self, 'id', None):
            # I have been saved and so am worthy of a redirect.
            if name in ('slug', 'title') and hasattr(self, name):
                old_name = 'old_' + name
                if not hasattr(self, old_name):
                    # Case insensitive comparison:
                    if getattr(self, name).lower() != value.lower():
                        # Save original value:
                        setattr(self, old_name, getattr(self, name))
                elif value == getattr(self, old_name):
                    # They changed the attr back to its original value.
                    delattr(self, old_name)
        super(Document, self).__setattr__(name, value)

    @property
    def content_parsed(self):
        if not self.current_revision:
            return ''
        return self.current_revision.content_parsed

    @property
    def language(self):
        return settings.LANGUAGES[self.locale.lower()]

    def get_absolute_url(self):
        return reverse('wiki.document', locale=self.locale, args=[self.slug])

    @classmethod
    def from_url(cls, url, required_locale=None, id_only=False,
                 check_host=True):
        """Return the approved Document the URL represents, None if there isn't
        one.

        Return None if the URL is a 404, the URL doesn't point to the right
        view, or the indicated document doesn't exist.

        To limit the universe of discourse to a certain locale, pass in a
        `required_locale`. To fetch only the ID of the returned Document, set
        `id_only` to True.

        If the URL has a host component, we assume it does not point to this
        host and thus does not point to a Document, because that would be a
        needlessly verbose way to specify an internal link. However, if you
        pass check_host=False, we assume the URL's host is the one serving
        Documents, which comes in handy for analytics whose metrics return
        host-having URLs.

        """
        try:
            components = _doc_components_from_url(
                url, required_locale=required_locale, check_host=check_host)
        except _NotDocumentView:
            return None
        if not components:
            return None
        locale, path, slug = components

        doc = cls.uncached
        if id_only:
            doc = doc.only('id')
        try:
            doc = doc.get(locale=locale, slug=slug)
        except cls.DoesNotExist:
            try:
                doc = doc.get(locale=settings.WIKI_DEFAULT_LANGUAGE, slug=slug)
                translation = doc.translated_to(locale)
                if translation:
                    return translation
                return doc
            except cls.DoesNotExist:
                return None
        return doc

    def redirect_url(self, source_locale=settings.LANGUAGE_CODE):
        """If I am a redirect, return the URL to which I redirect.

        Otherwise, return None.

        """
        # If a document starts with REDIRECT_HTML and contains any <a> tags
        # with hrefs, return the href of the first one. This trick saves us
        # from having to parse the HTML every time.
        if self.html.startswith(REDIRECT_HTML):
            anchors = PyQuery(self.html)('a[href]')
            if anchors:
                # Articles with a redirect have a link that has the locale
                # hardcoded into it, and so by simply redirecting to the given
                # link, we end up possibly losing the locale. So, instead,
                # we strip out the locale and replace it with the original
                # source locale only in the case where an article is going
                # from one locale and redirecting it to a different one.
                # This only applies when it's a non-default locale because we
                # don't want to override the redirects that are forcibly
                # changing to (or staying within) a specific locale.
                full_url = anchors[0].get('href')
                (dest_locale, url) = split_path(full_url)
                if (source_locale != dest_locale
                    and dest_locale == settings.LANGUAGE_CODE):
                    return '/' + source_locale + '/' + url
                return full_url

    def redirect_document(self):
        """If I am a redirect to a Document, return that Document.

        Otherwise, return None.

        """
        url = self.redirect_url()
        if url:
            return self.from_url(url)

    def __unicode__(self):
        return '[%s] %s' % (self.locale, self.title)

    def allows_vote(self, request):
        """Return whether `user` can vote on this document."""
        return (not self.is_archived and self.current_revision and
                not self.current_revision.has_voted(request) and
                not self.redirect_document())

    def translated_to(self, locale):
        """Return the translation of me to the given locale.

        If there is no such Document, return None.

        """
        if self.locale != settings.WIKI_DEFAULT_LANGUAGE:
            raise NotImplementedError('translated_to() is implemented only on'
                                      'Documents in the default language so'
                                      'far.')
        try:
            return Document.objects.get(locale=locale, parent=self)
        except Document.DoesNotExist:
            return None

    @property
    def original(self):
        """Return the document I was translated from or, if none, myself."""
        return self.parent or self

    def localizable_or_latest_revision(self, include_rejected=False):
        """Return latest ready-to-localize revision if there is one,
        else the latest approved revision if there is one,
        else the latest unrejected (unreviewed) revision if there is one,
        else None.

        include_rejected -- If true, fall back to the latest rejected
            revision if all else fails.

        """
        def latest(queryset):
            """Return the latest item from a queryset (by ID).

            Return None if the queryset is empty.

            """
            try:
                return queryset.order_by('-id')[0:1].get()
            except ObjectDoesNotExist:  # Catching IndexError seems overbroad.
                return None

        rev = self.latest_localizable_revision
        if not rev or not self.is_localizable:
            rejected = Q(is_approved=False, reviewed__isnull=False)

            # Try latest approved revision:
            rev = (latest(self.revisions.filter(is_approved=True)) or
                   # No approved revs. Try unrejected:
                   latest(self.revisions.exclude(rejected)) or
                   # No unrejected revs. Maybe fall back to rejected:
                   (latest(self.revisions) if include_rejected else None))
        return rev

    def is_outdated(self, level=MEDIUM_SIGNIFICANCE):
        """Return whether an update of a given magnitude has occured
        to the parent document since this translation had an approved
        update and such revision is ready for l10n.

        If this is not a translation or has never been approved, return
        False.

        level: The significance of an edit that is "enough". Defaults to
            MEDIUM_SIGNIFICANCE.

        """
        if not (self.parent and self.current_revision):
            return False

        based_on_id = self.current_revision.based_on_id
        more_filters = {'id__gt': based_on_id} if based_on_id else {}

        return self.parent.revisions.filter(
            is_approved=True, is_ready_for_localization=True,
            significance__gte=level, **more_filters).exists()

    def is_majorly_outdated(self):
        """Return whether a MAJOR_SIGNIFICANCE-level update has occurred to the
        parent document since this translation had an approved update and such
        revision is ready for l10n.

        If this is not a translation or has never been approved, return False.

        """
        return self.is_outdated(level=MAJOR_SIGNIFICANCE)

    def is_watched_by(self, user):
        """Return whether `user` is notified of edits to me."""
        from kitsune.wiki.events import EditDocumentEvent
        return EditDocumentEvent.is_notifying(user, self)

    def get_topics(self, uncached=False):
        """Return the list of topics that apply to this document.

        If the document has a parent, it inherits the parent's topics.
        """
        if self.parent:
            return self.parent.get_topics()
        if uncached:
            q = Topic.uncached
        else:
            q = Topic.objects
        return q.filter(document=self)

    # Remove get_topics above and replace it with this one.
    def get_new_topics(self, uncached=False):
        """Return the list of new topics that apply to this document.

        If the document has a parent, it inherits the parent's topics.
        """
        if self.parent:
            return self.parent.get_new_topics()
        if uncached:
            q = NewTopic.uncached
        else:
            q = NewTopic.objects
        return q.filter(document=self)

    def get_products(self, uncached=False):
        """Return the list of products that apply to this document.

        If the document has a parent, it inherits the parent's products.
        """
        if self.parent:
            return self.parent.get_products()
        if uncached:
            q = Product.uncached
        else:
            q = Product.objects
        return q.filter(document=self)

    @property
    def recent_helpful_votes(self):
        """Return the number of helpful votes in the last 30 days."""
        start = datetime.now() - timedelta(days=30)
        return HelpfulVote.objects.filter(
            revision__document=self, created__gt=start, helpful=True).count()

    @property
    def related_documents(self):
        """Return documents that are 'morelikethis' one."""
        # Only documents in default IA categories have related.
        if (self.redirect_url() or not self.current_revision or
            self.category not in settings.IA_DEFAULT_CATEGORIES):
            return []

        # First try to get the results from the cache
        key = 'wiki_document:related_docs:%s' % self.id
        documents = cache.get(key)
        if documents is not None:
            statsd.incr('wiki.related_documents.cache.hit')
            log.debug('Getting MLT for {doc} from cache.'
                .format(doc=repr(self)))
            return documents

        try:
            statsd.incr('wiki.related_documents.cache.miss')
            mt = self.get_mapping_type()
            documents = mt.morelikethis(
                self.id,
                s=mt.search().filter(
                    document_locale=self.locale,
                    document_is_archived=False,
                    document_category__in=settings.IA_DEFAULT_CATEGORIES),
                fields=[
                    'document_title',
                    'document_summary',
                    'document_content'])[:3]
            cache.add(key, documents)
        except ES_EXCEPTIONS as exc:
            statsd.incr('wiki.related_documents.esexception')
            log.error('ES MLT {err} related_documents for {doc}'.format(
                    doc=repr(self), err=str(exc)))
            documents = []

        return documents

    @property
    def related_questions(self):
        """Return questions that are 'morelikethis' document."""
        # Only documents in default IA categories have related.
        if (self.redirect_url() or not self.current_revision or
            self.category not in settings.IA_DEFAULT_CATEGORIES):
            return []

        # First try to get the results from the cache
        key = 'wiki_document:related_questions:%s' % self.id
        questions = cache.get(key)
        if questions is not None:
            statsd.incr('wiki.related_questions.cache.hit')
            log.debug('Getting MLT questions for {doc} from cache.'
                .format(doc=repr(self)))
            return questions

        try:
            statsd.incr('wiki.related_questions.cache.miss')
            max_age = settings.SEARCH_DEFAULT_MAX_QUESTION_AGE
            start_date = int(time.time()) - max_age

            s = Question.get_mapping_type().search()
            questions = s.values_dict('id', 'question_title', 'url').filter(
                    question_locale=self.locale,
                    product__in=[p.slug for p in self.get_products()],
                    question_has_helpful=True,
                    created__gte=start_date
                ).query(
                    __mlt={
                        'fields': ['question_title', 'question_content'],
                        'like_text': self.title,
                        'min_term_freq': 1,
                        'min_doc_freq': 1,
                    }
                )[:3]
            questions = list(questions)
            cache.add(key, questions)
        except ES_EXCEPTIONS as exc:
            statsd.incr('wiki.related_questions.esexception')
            log.error('ES MLT {err} related_questions for {doc}'.format(
                    doc=repr(self), err=str(exc)))
            questions = []

        return questions

    @classmethod
    def get_mapping_type(cls):
        return DocumentMappingType

    def parse_and_calculate_links(self):
        """Calculate What Links Here data for links going out from this.

        Also returns a parsed version of the current html, because that
        is a byproduct of the process, and is useful.
        """
        if not self.current_revision:
            return ''

        # Remove "what links here" reverse links, because they might be
        # stale and re-rendering will re-add them. This cannot be done
        # reliably in the parser's parse() function, because that is
        # often called multiple times per document.
        self.links_from().delete()

        from kitsune.wiki.parser import wiki_to_html, WhatLinksHereParser
        return wiki_to_html(self.current_revision.content,
                            locale=self.locale,
                            doc_id=self.id,
                            parser_cls=WhatLinksHereParser)

    def links_from(self):
        """Get a query set of links that are from this document to another."""
        return DocumentLink.objects.filter(linked_from=self)

    def links_to(self):
        """Get a query set of links that are from another document to this."""
        return DocumentLink.objects.filter(linked_to=self)

    def add_link_to(self, linked_to, kind):
        """Create a DocumentLink to another Document."""
        try:
            DocumentLink(linked_from=self,
                         linked_to=linked_to,
                         kind=kind).save()
        except IntegrityError:
            # This link already exists, ok.
            pass


@register_mapping_type
class DocumentMappingType(SearchMappingType):
    @classmethod
    def get_model(cls):
        return Document

    @classmethod
    def get_query_fields(cls):
        return ['document_title',
                'document_content',
                'document_summary',
                'document_keywords']

    @classmethod
    def get_mapping(cls):
        return {
            'properties': {
                'id': {'type': 'long'},
                'model': {'type': 'string', 'index': 'not_analyzed'},
                'url': {'type': 'string', 'index': 'not_analyzed'},
                'indexed_on': {'type': 'integer'},
                'updated': {'type': 'integer'},

                'product': {'type': 'string', 'index': 'not_analyzed'},
                'topic': {'type': 'string', 'index': 'not_analyzed'},

                'document_title': {'type': 'string', 'analyzer': 'snowball'},
                'document_locale': {'type': 'string', 'index': 'not_analyzed'},
                'document_current_id': {'type': 'integer'},
                'document_parent_id': {'type': 'integer'},
                'document_content': {'type': 'string', 'analyzer': 'snowball',
                                     'store': 'yes',
                                     'term_vector': 'with_positions_offsets'},
                'document_category': {'type': 'integer'},
                'document_slug': {'type': 'string', 'index': 'not_analyzed'},
                'document_is_archived': {'type': 'boolean'},
                'document_summary': {'type': 'string', 'analyzer': 'snowball'},
                'document_keywords': {'type': 'string', 'analyzer': 'snowball'},
                'document_recent_helpful_votes': {'type': 'integer'}
            }
        }

    @classmethod
    def extract_document(cls, obj_id, obj=None):
        if obj is None:
            model = cls.get_model()
            obj = model.uncached.select_related(
                'current_revision', 'parent').get(pk=obj_id)

        if obj.html.startswith(REDIRECT_HTML):
            # It's possible this document is indexed and was turned
            # into a redirect, so now we want to explicitly unindex
            # it. The way we do that is by throwing an exception
            # which gets handled by the indexing machinery.
            raise UnindexMeBro()

        d = {}
        d['id'] = obj.id
        d['model'] = cls.get_mapping_type_name()
        d['url'] = obj.get_absolute_url()
        d['indexed_on'] = int(time.time())

        # For now, union the slugs of the old topics and new topics.
        # .....What could go wrong?
        # TODO: fix this when we remove old topics.
        topics = list(set(
            [t.slug for t in obj.get_topics(True)] +
            [t.slug for t in obj.get_new_topics(True)]))
        d['topic'] = topics
        d['product'] = [p.slug for p in obj.get_products(True)]

        d['document_title'] = obj.title
        d['document_locale'] = obj.locale
        d['document_parent_id'] = obj.parent.id if obj.parent else None
        d['document_content'] = obj.html
        d['document_category'] = obj.category
        d['document_slug'] = obj.slug
        d['document_is_archived'] = obj.is_archived

        if obj.current_revision is not None:
            d['document_summary'] = obj.current_revision.summary
            d['document_keywords'] = obj.current_revision.keywords
            d['updated'] = int(time.mktime(
                    obj.current_revision.created.timetuple()))
            d['document_current_id'] = obj.current_revision.id
            d['document_recent_helpful_votes'] = obj.recent_helpful_votes
        else:
            d['document_summary'] = None
            d['document_keywords'] = None
            d['updated'] = None
            d['document_current_id'] = None
            d['document_recent_helpful_votes'] = 0

        # Don't query for helpful votes if the document doesn't have a current
        # revision, or is a template, or is a redirect, or is in Navigation
        # category (50).
        if (obj.current_revision and
            not obj.is_template and
            not obj.html.startswith(REDIRECT_HTML) and
            not obj.category == 50):
            d['document_recent_helpful_votes'] = obj.recent_helpful_votes
        else:
            d['document_recent_helpful_votes'] = 0

        return d

    @classmethod
    def get_indexable(cls):
        # This function returns all the indexable things, but we
        # really need to handle the case where something was indexable
        # and isn't anymore. Given that, this returns everything that
        # has a revision.
        indexable = super(cls, cls).get_indexable()
        indexable = indexable.filter(current_revision__isnull=False)
        return indexable

    @classmethod
    def index(cls, document, **kwargs):
        # If there are no revisions or the current revision is a
        # redirect, we want to remove it from the index.
        if (document['document_current_id'] is None or
            document['document_content'].startswith(REDIRECT_HTML)):

            cls.unindex(document['id'], es=kwargs.get('es', None))
            return

        super(cls, cls).index(document, **kwargs)


register_for_indexing('wiki', Document)
register_for_indexing(
    'wiki',
    Document.topics.through,
    m2m=True)
register_for_indexing(
    'wiki',
    Document.new_topics.through,
    m2m=True)
register_for_indexing(
    'wiki',
    Document.products.through,
    m2m=True)


MAX_REVISION_COMMENT_LENGTH = 255


class Revision(ModelBase):
    """A revision of a localized knowledgebase document"""
    document = models.ForeignKey(Document, related_name='revisions')
    summary = models.TextField()  # wiki markup
    content = models.TextField()  # wiki markup

    # Keywords are used mostly to affect search rankings. Moderators may not
    # have the language expertise to translate keywords, so we put them in the
    # Revision so the translators can handle them:
    keywords = models.CharField(max_length=255, blank=True)

    created = models.DateTimeField(default=datetime.now)
    reviewed = models.DateTimeField(null=True)

    # The significance of the initial revision of a document is NULL.
    significance = models.IntegerField(choices=SIGNIFICANCES, null=True)

    comment = models.CharField(max_length=MAX_REVISION_COMMENT_LENGTH)
    reviewer = models.ForeignKey(User, related_name='reviewed_revisions',
                                 null=True)
    creator = models.ForeignKey(User, related_name='created_revisions')
    is_approved = models.BooleanField(default=False, db_index=True)

    # The default locale's rev that was the latest ready-for-l10n one when the
    # Edit button was hit to begin creating this revision. If there was none,
    # this is simply the latest of the default locale's revs as of that time.
    # Used to determine whether localizations are out of date.
    based_on = models.ForeignKey('self', null=True, blank=True)
    # TODO: limit_choices_to={'document__locale':
    # settings.WIKI_DEFAULT_LANGUAGE} is a start but not sufficient.

    # Is both approved and marked as ready for translation (which will result
    # in the translation UI considering it when looking for the latest
    # translatable version). If is_approved=False or this revision belongs to a
    # non-default-language Document, this must be False.
    is_ready_for_localization = models.BooleanField(default=False)
    readied_for_localization = models.DateTimeField(null=True)
    readied_for_localization_by = models.ForeignKey(
        User, related_name='readied_for_l10n_revisions', null=True)

    class Meta(object):
        permissions = [('review_revision', 'Can review a revision'),
                       ('mark_ready_for_l10n',
                        'Can mark revision as ready for localization'),
                       ('edit_keywords', 'Can edit keywords')]

    def _based_on_is_clean(self):
        """Return a tuple: (the correct value of based_on, whether the old
        value was correct).

        based_on must be a revision of the English version of the document. If
        based_on is not already set when this is called, the return value
        defaults to something reasonable.

        """
        original = self.document.original
        if self.based_on and self.based_on.document != original:
            # based_on is set and points to the wrong doc. The following is
            # then the most likely helpful value:
            return original.localizable_or_latest_revision(), False
        # Even None is permissible, for example in the case of a brand new doc.
        return self.based_on, True

    def clean(self):
        """Ensure based_on is valid & police is_ready/is_approved invariant."""
        # All of the cleaning herein should be unnecessary unless the user
        # messes with hidden form data.
        try:
            self.document and self.document.original
        except Document.DoesNotExist:
            # For clean()ing forms that don't have a document instance behind
            # them yet
            self.based_on = None
        else:
            based_on, is_clean = self._based_on_is_clean()
            if not is_clean:
                old = self.based_on
                self.based_on = based_on  # Be nice and guess a correct value.
                # TODO(erik): This error message ignores non-translations.
                raise ValidationError(_('A revision must be based on the '
                    'English article. Revision ID %(id)s does not fit this'
                    ' criterion.') %
                    dict(id=old.id))

        if not self.can_be_readied_for_localization():
            self.is_ready_for_localization = False

    def save(self, *args, **kwargs):
        _, is_clean = self._based_on_is_clean()
        if not is_clean:  # No more Mister Nice Guy
            # TODO(erik): This error message ignores non-translations.
            raise ProgrammingError('Revision.based_on must be None or refer '
                                   'to a revision of the default-'
                                   'language document.')

        super(Revision, self).save(*args, **kwargs)

        # When a revision is approved, re-cache the document's html content
        # and update document contributors
        if self.is_approved and (
                not self.document.current_revision or
                self.document.current_revision.id < self.id):
            # Determine if there are new contributors and add them to the list
            contributors = self.document.contributors.all()
            # Exclude all explicitly rejected revisions
            new_revs = self.document.revisions.exclude(
                reviewed__isnull=False, is_approved=False)
            if self.document.current_revision:
                new_revs = new_revs.filter(
                    id__gt=self.document.current_revision.id)
            new_contributors = set([r.creator
                for r in new_revs.select_related('creator')])
            for user in new_contributors:
                if user not in contributors:
                    self.document.contributors.add(user)

            # Update document denormalized fields
            if self.is_ready_for_localization:
                self.document.latest_localizable_revision = self
            self.document.html = self.content_parsed
            self.document.current_revision = self
            self.document.save()
        elif (self.is_ready_for_localization and
              (not self.document.latest_localizable_revision or
               self.id > self.document.latest_localizable_revision.id)):
            # We are marking a newer revision as ready for l10n.
            # Update the denormalized field on the document.
            self.document.latest_localizable_revision = self
            self.document.save()

    def delete(self, *args, **kwargs):
        """Dodge cascading delete of documents and other revisions."""
        def latest_revision(excluded_rev, constraint):
            """Return the largest-ID'd revision meeting the given constraint
            and excluding the given revision, or None if there is none."""
            revs = document.revisions.filter(constraint).exclude(
                pk=excluded_rev.pk).order_by('-id')[:1]
            try:
                # Academic TODO: There's probably a way to keep the QuerySet
                # lazy all the way through the update() call.
                return revs[0]
            except IndexError:
                return None

        Revision.objects.filter(based_on=self).update(based_on=None)
        document = self.document

        # If the current_revision is being deleted, try to update it to the
        # previous approved revision:
        if document.current_revision == self:
            new_current = latest_revision(self, Q(is_approved=True))
            document.update(
                current_revision=new_current,
                html=new_current.content_parsed if new_current else '')

        # Likewise, step the latest_localizable_revision field backward if
        # we're deleting that revision:
        if document.latest_localizable_revision == self:
            document.update(latest_localizable_revision=latest_revision(
                self, Q(is_approved=True, is_ready_for_localization=True)))

        super(Revision, self).delete(*args, **kwargs)

    def has_voted(self, request):
        """Did the user already vote for this revision?"""
        if request.user.is_authenticated():
            qs = HelpfulVote.objects.filter(revision=self,
                                            creator=request.user)
        elif request.anonymous.has_id:
            anon_id = request.anonymous.anonymous_id
            qs = HelpfulVote.objects.filter(revision=self,
                                            anonymous_id=anon_id)
        else:
            return False

        return qs.exists()

    def __unicode__(self):
        return u'[%s] %s #%s: %s' % (self.document.locale,
                                     self.document.title,
                                     self.id, self.content[:50])

    @property
    def content_parsed(self):
        from kitsune.wiki.parser import wiki_to_html
        return wiki_to_html(self.content, locale=self.document.locale,
                            doc_id=self.document.id)

    def can_be_readied_for_localization(self):
        """Return whether this revision has the prerequisites necessary for the
        user to mark it as ready for localization."""
        # If not is_approved, can't be is_ready. TODO: think about using a
        # single field with more states.
        # Also, if significance is trivial, it shouldn't be translated.
        return (self.is_approved and
                self.significance > TYPO_SIGNIFICANCE and
                self.document.locale == settings.WIKI_DEFAULT_LANGUAGE)

    def get_absolute_url(self):
        return reverse('wiki.revision', locale=self.document.locale,
                       args=[self.document.slug, self.id])

    @property
    def previous(self):
        """Get the revision that came before this in the document's history."""
        older_revs = Revision.objects.filter(document=self.document,
                                             id__lt=self.id,
                                             is_approved=True)
        older_revs = older_revs.order_by('-created')
        try:
            return older_revs[0]
        except IndexError:
            return None


class HelpfulVote(ModelBase):
    """Helpful or Not Helpful vote on Revision."""
    revision = models.ForeignKey(Revision, related_name='poll_votes')
    helpful = models.BooleanField(default=False)
    created = models.DateTimeField(default=datetime.now, db_index=True)
    creator = models.ForeignKey(User, related_name='poll_votes', null=True)
    anonymous_id = models.CharField(max_length=40, db_index=True)
    user_agent = models.CharField(max_length=1000)

    def add_metadata(self, key, value):
        HelpfulVoteMetadata.objects.create(vote=self, key=key, value=value)


class HelpfulVoteMetadata(ModelBase):
    """Metadata for article votes."""
    vote = models.ForeignKey(HelpfulVote, related_name='metadata')
    key = models.CharField(max_length=40, db_index=True)
    value = models.CharField(max_length=1000)


class ImportantDate(ModelBase):
    """Important date that shows up globally on metrics graphs."""
    text = models.CharField(max_length=100)
    date = models.DateField(db_index=True)


class Locale(ModelBase):
    """A locale supported in the KB."""
    locale = models.CharField(max_length=7, db_index=True)
    leaders = models.ManyToManyField(
        User, blank=True, related_name='locales_leader')
    reviewers = models.ManyToManyField(
        User, blank=True, related_name='locales_reviewer')
    editors = models.ManyToManyField(
        User, blank=True, related_name='locales_editor')

    class Meta:
        ordering = ['locale']

    def get_absolute_url(self):
        return reverse('wiki.locale_details', args=[self.locale])

    def __unicode__(self):
        return self.locale


class DocumentLink(ModelBase):
    """Model a link between documents.

    If article A contains [[Link:B]], then `linked_to` is B,
    `linked_from` is A, and kind is 'link'.
    """
    linked_to = models.ForeignKey(Document,
                                  related_name='documentlink_from_set')
    linked_from = models.ForeignKey(Document,
                                    related_name='documentlink_to_set')
    kind = models.CharField(max_length=16)

    class Meta:
        unique_together = ('linked_from', 'linked_to')

    def __repr__(self):
        return ('<DocumentLink: %s from %r to %r>' %
                (self.kind, self.linked_from, self.linked_to))


def _doc_components_from_url(url, required_locale=None, check_host=True):
    """Return (locale, path, slug) if URL is a Document, False otherwise.

    If URL doesn't even point to the document view, raise _NotDocumentView.

    """
    # Extract locale and path from URL:
    parsed = urlparse(url)  # Never has errors AFAICT
    if check_host and parsed.netloc:
        return False
    locale, path = split_path(parsed.path)
    if required_locale and locale != required_locale:
        return False
    path = '/' + path

    try:
        view, view_args, view_kwargs = resolve(path)
    except Http404:
        return False

    import kitsune.wiki.views  # Views import models; models import views.
    if view != kitsune.wiki.views.document:
        raise _NotDocumentView
    return locale, path, view_kwargs['document_slug']


def points_to_document_view(url, required_locale=None):
    """Return whether a URL reverses to the document view.

    To limit the universe of discourse to a certain locale, pass in a
    `required_locale`.

    """
    try:
        return not not _doc_components_from_url(
            url, required_locale=required_locale)
    except _NotDocumentView:
        return False


def user_num_documents(user):
    """Count the number of documents a user has contributed to. """
    return Document.objects.filter(
        revisions__creator=user).distinct().count()


def user_documents(user):
    """Return the documents a user has contributed to."""
    return Document.objects.filter(
        revisions__creator=user).distinct()