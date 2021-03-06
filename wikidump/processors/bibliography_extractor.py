"""Extract sections which are to be considered bibliography."""
import collections
import datetime
import functools

import fuzzywuzzy.process
import jsonable
import more_itertools
import mwxml
from typing import Iterable, Iterator, Mapping, NamedTuple, Optional

from .. import dumper, extractors, languages, utils

FUZZY_MATCH_CUTOFF = 91      # between 0, 100

features_template = '''
<%!
    from itertools import groupby
    def groupby_action(diff):
        return groupby(diff, lambda d: d.action)
%>
<%def name="element_if_exists(name, text)" filter="trim">
    % if text is not None:
        <${name}>${text | x}</${name}>
    % endif
</%def>
<%def name="contributor_if_exists(user)" filter="trim">
    % if user:
            <contributor>
            % if user.id is not None:
                ${element_if_exists('username', user.text)}
                ${element_if_exists('id', user.id)}
            % else:
                ${element_if_exists('ip', user.text)}
            % endif
            </contributor>
    % endif
</%def>
<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.10/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://www.mediawiki.org/xml/export-0.10/ http://github.com/youtux/wikidump/blob/master/schemas/wikidump-0.1-mediawiki-0.10.xsd" version="0.10" xml:lang="en">
    <siteinfo>
        <sitename>${siteinfo.name | x}</sitename>
        <dbname>${siteinfo.dbname | x}</dbname>
        <base>${siteinfo.base | x}</base>
        <generator>${generator | x}</generator>
        <case>${siteinfo.case | x}</case>
        <namespaces>
            % for namespace in siteinfo.namespaces:
            <namespace key="${namespace.id | x}" case="${namespace.case | x}">${namespace.name | x}</namespace>
            % endfor
        </namespaces>
    </siteinfo>
    % for page in pages:
    <page>
        <title>${page.title | x}</title>
        <ns>${page.namespace | x}</ns>
        <id>${page.id | x}</id>
        % for revision in page.revisions:
        <revision>
            <id>${revision.id | x}</id>
            % if revision.parent_id is not None:
            <parentid>${revision.parent_id | x}</parentid>
            % endif
            <timestamp>${revision.timestamp | x}</timestamp>
            ${contributor_if_exists(revision.user)}
            % if revision.minor:
            <minor />
            % endif
            <comment>${revision.comment | x}</comment>
            <model>${revision.model | x}</model>
            <format>${revision.format | x}</format>
            <text xml:space="preserve">${revision.text | x}</text>
            <sha1>dummy</sha1>
            <sections>
                % for section in revision.sections:
                    <section name="${section.name | x}" level="${section.level | x}" />
                % endfor
            </sections>
        </revision>
        % endfor
    </page>
    % endfor
</mediawiki>
'''

stats_template = '''
<stats>
    <performance>
        <start_time>${stats['performance']['start_time'] | x}</start_time>
        <end_time>${stats['performance']['end_time'] | x}</end_time>
        <revisions_analyzed>${stats['performance']['revisions_analyzed'] | x}</revisions_analyzed>
        <pages_analyzed>${stats['performance']['pages_analyzed'] | x}</pages_analyzed>
    </performance>
    <extracted-section-names>
        % for key in ['global', 'last_revision']:
        <${key}>
            % for section_name, count in stats['section_names'][key].most_common():
            <section name="${section_name | x}" count="${count}" />
            % endfor
        </${key}>
        % endfor
    </extracted-section-names>
</stats>
'''


Revision = NamedTuple('Revision', [
    ('id', int),
    ('parent_id', int),
    ('user', Optional[mwxml.Revision.User]),
    ('minor', bool),
    ('comment', str),
    ('model', str),
    ('format', str),
    ('timestamp', jsonable.Type),
    ('text', str),
    ('sections', Iterable[extractors.misc.Section])
])


Page = NamedTuple('Page', [
    ('id', str),
    ('namespace', int),
    ('title', str),
    ('revisions', Iterable[Revision]),
])


# TODO: instead of comparing section_name to a bib synonym,
# search all the possible bib synonyms in the section name
@functools.lru_cache(maxsize=500)
def is_bibliography(
        section_name: str,
        language: str,
        score_cutoff: int=FUZZY_MATCH_CUTOFF) -> bool:
    """Check whether a section is a bibliography."""
    bibliography_synonyms = languages.bibliography[language]
    match = fuzzywuzzy.process.extractOne(
        section_name,
        bibliography_synonyms,
        score_cutoff=score_cutoff,
    )
    return bool(match)


def extract_revisions(
        mw_page: mwxml.Page,
        language: str,
        stats: Mapping,
        only_last_revision: bool) -> Iterator[Revision]:
    """Extract the sections which are bibliography from the revisions."""
    section_names_stats = stats['section_names']
    revisions = more_itertools.peekable(mw_page)
    for mw_revision in revisions:
        utils.dot()

        is_last_revision = not utils.has_next(revisions)
        if only_last_revision and not is_last_revision:
            continue

        text = utils.remove_comments(mw_revision.text or '')

        sections = (section for section, _ in extractors.sections(text))

        bibliography_sections = list(
            section for section in sections
            if is_bibliography(section.name, language)
        )

        for section in bibliography_sections:
            section_names_stats['global'][section.name] += 1
            if is_last_revision:
                section_names_stats['last_revision'][section.name] += 1
        # TODO: use section.fullbody
        text = "".join(section.full_body for section in bibliography_sections)

        yield Revision(
            id=mw_revision.id,
            parent_id=mw_revision.parent_id,
            user=mw_revision.user,
            minor=mw_revision.minor,
            comment=mw_revision.comment,
            model=mw_revision.model,
            format=mw_revision.format,
            timestamp=mw_revision.timestamp.to_json(),
            text=text,
            sections=bibliography_sections,
        )

        stats['performance']['revisions_analyzed'] += 1


def extract_pages(
        dump: Iterable[mwxml.Page],
        language: str,
        stats: Mapping,
        only_last_revision: bool) -> Iterator[Page]:
    """Extract revisions from a page."""
    for mw_page in dump:
        utils.log("Processing", mw_page.title)

        # Skip non-articles
        if mw_page.namespace != 0:
            utils.log('Skipped (namespace != 0)')
            continue

        revisions_generator = extract_revisions(
            mw_page,
            language=language,
            stats=stats,
            only_last_revision=only_last_revision,
        )

        yield Page(
            id=mw_page.id,
            namespace=mw_page.namespace,
            title=mw_page.title,
            revisions=revisions_generator,
        )
        stats['performance']['pages_analyzed'] += 1


def configure_subparsers(subparsers):
    """Configure a new subparser."""
    parser = subparsers.add_parser(
        'extract-bibliography',
        help='Extract only sections may be a bibliography',
    )
    parser.add_argument(
        '-l', '--language',
        choices=languages.supported,
        required=True,
        help='The language of the dump.',
    )
    parser.add_argument(
        '--only-last-revision',
        action='store_true',
        help='Consider only the last revision for each page.',
    )
    parser.set_defaults(func=main)


def main(
        dump: Iterable[mwxml.Page],
        features_output_h,
        stats_output_h,
        args) -> None:
    """Main function that parses the arguments and writes the output."""
    stats = {
        'performance': {
            'start_time': None,
            'end_time': None,
            'revisions_analyzed': 0,
            'pages_analyzed': 0,
        },
        'section_names': {
            'global': collections.Counter(),
            'last_revision': collections.Counter(),
        },
    }

    pages_generator = extract_pages(
        dump,
        language=args.language,
        stats=stats,
        only_last_revision=args.only_last_revision,
    )

    with features_output_h:
        stats['performance']['start_time'] = datetime.datetime.utcnow()
        dumper.render_template(
            features_template,
            output_handler=features_output_h,
            siteinfo=dump.site_info,
            pages=pages_generator,
            generator='youtux/wikidump',
        )
        stats['performance']['end_time'] = datetime.datetime.utcnow()

    with stats_output_h:
        dumper.render_template(
            stats_template,
            stats_output_h,
            stats=stats,
        )
