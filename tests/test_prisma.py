from canary import prisma


def test_catalogue_contains_each_official_item_once():
    main = ('1 2 3 4 5 6 7 8 9 10a 10b 11 12 13a 13b 13c 13d 13e 13f 14 15 '
            '16a 16b 17 18 19 20a 20b 20c 20d 21 22 23a 23b 23c 23d 24a 24b 24c 25 26 27').split()
    abstract = [f'abstract-{i}' for i in range(1, 13)]
    items = prisma.catalogue()['items']
    assert [item['id'] for item in items] == main + abstract
    assert all(item['title'] and all(item['elements']) and item['elements'] for item in items)
    assert items[1]['see_also'] == abstract
